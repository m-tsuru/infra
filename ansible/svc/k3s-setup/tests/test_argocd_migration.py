"""Offline migration tests: execute only localhost set_fact/assert actions, never roles."""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[2]
ROLE = SERVICE / "roles/argocd_bootstrap"
GITOPS = SERVICE / "gitops"
GUARD_REGISTERS = {
    "_argocd_flux_crds",
    "_argocd_flux_kustomizations",
    "_argocd_flux_helmreleases",
    "_argocd_flux_deployments",
    "_argocd_flux_pods",
    "_argocd_flux_source",
}
BOOTSTRAP_REGISTERS = {"_argocd_crd_probe", "_argocd_root_probe"}
MARKERS = ("_test_stage_allocated", "_test_install_applied", "_test_root_applied")
OPT_IN = "Require an explicit opt-in before replacing an existing Flux installation"
KUSTOMIZATION_GUARD = (
    "Verify every Flux Kustomization is protected and matches the existing shared manifests"
)
SOURCE_GUARD = "Reject a handover from an unrelated or transformed Git source"
CONTROLLER_GUARD = "Verify Flux controllers cannot resume reconciliation during adoption"
POD_GUARD = "Require Flux controller Pods to be fully stopped"
HELM_GUARD = "Reject unsupported Flux HelmRelease migration"


def load_yaml(path):
    return yaml.safe_load(path.read_text())


def documents(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc is not None]


def walk_tasks(tasks):
    for task in tasks:
        yield task
        for branch in ("block", "rescue", "always"):
            yield from walk_tasks(task.get(branch, []))


def local_actions_only(tasks):
    """Fail closed if future role changes leak executable actions into this harness."""
    for task in walk_tasks(tasks):
        actions = [key for key in task if key.startswith("ansible.builtin.")]
        if "block" in task:
            if actions:
                raise AssertionError("A harness block must not also contain an action")
        elif len(actions) != 1 or actions[0] not in {
            "ansible.builtin.set_fact", "ansible.builtin.assert"
        }:
            raise AssertionError(f"Non-local harness action: {task}")
        if any(key in task for key in ("delegate_to", "local_action", "action")):
            raise AssertionError("Delegation and unqualified actions are forbidden")
    rendered = yaml.safe_dump(tasks)
    if re.search(r"\b(?:lookup|query)\s*\(", rendered):
        raise AssertionError("The offline play must not perform lookups")


def mock_probes(tasks, probes, allowed_registers):
    mocked = []
    for original in tasks:
        task = copy.deepcopy(original)
        if "block" in task:
            task["block"] = mock_probes(task["block"], probes, allowed_registers)
        elif "ansible.builtin.command" in task:
            register = task.get("register")
            argv = task["ansible.builtin.command"].get("argv", [])
            if register not in allowed_registers or argv[:3] != [
                "/usr/local/bin/k3s", "kubectl", "get"
            ]:
                raise AssertionError(f"Unexpected command probe: {task['name']}")
            task = {
                "name": "Mock probe: " + original["name"],
                "ansible.builtin.set_fact": {register: copy.deepcopy(probes[register])},
            }
            if "when" in original:
                task["when"] = copy.deepcopy(original["when"])
        mocked.append(task)
    local_actions_only(mocked)
    return mocked


def bootstrap_decisions(tasks, probes):
    """Project real conditions onto local markers; never include the bootstrap role."""
    projected = []
    for task in tasks:
        if "block" in task:
            children = bootstrap_decisions(task["block"], probes)
            if children:
                block = {"name": task["name"], "block": children}
                if "when" in task:
                    block["when"] = copy.deepcopy(task["when"])
                projected.append(block)
            continue
        if task.get("register") in BOOTSTRAP_REGISTERS:
            projected.extend(mock_probes([task], probes, BOOTSTRAP_REGISTERS))
            continue
        if "_argocd_install_needed" in task.get("ansible.builtin.set_fact", {}):
            projected.append(copy.deepcopy(task))
            continue
        marker = None
        if "ansible.builtin.tempfile" in task:
            marker = "_test_stage_allocated"
        else:
            argv = task.get("ansible.builtin.command", {}).get("argv", [])
            if isinstance(argv, list) and argv[:3] == ["/usr/local/bin/k3s", "kubectl", "apply"]:
                if "--kustomize" in argv:
                    marker = "_test_install_applied"
                elif any("/applications/root.yaml" in arg for arg in argv):
                    marker = "_test_root_applied"
                else:
                    raise AssertionError(f"Unaccounted bootstrap apply: {task['name']}")
        if marker:
            replacement = {
                "name": "Mark decision: " + task["name"],
                "ansible.builtin.set_fact": {marker: True},
            }
            if "when" in task:
                replacement["when"] = copy.deepcopy(task["when"])
            projected.append(replacement)
    local_actions_only(projected)
    return projected


def json_probe(items):
    return {"rc": 0, "stdout": json.dumps({"items": items})}


def cluster_probes(flux=False, empty_flux_crds=False):
    sync = documents(GITOPS / "clusters/local/flux-system/gotk-sync.yaml")
    source = next(doc for doc in sync if doc["kind"] == "GitRepository")
    kustomizations = [next(doc for doc in sync if doc["kind"] == "Kustomization")]
    kustomizations += [load_yaml(GITOPS / "clusters/local" / name)
                       for name in ("sealed-secrets.yaml", "apps.yaml")]
    for resource in kustomizations:
        resource["spec"].update(suspend=True, prune=False, deletionPolicy="Orphan")
    controllers = [
        {"metadata": {"namespace": "flux-system", "name": name,
                      "labels": {"app.kubernetes.io/part-of": "flux"}},
         "spec": {"replicas": 0}, "status": {"replicas": 0}}
        for name in ("source-controller", "kustomize-controller", "helm-controller",
                     "notification-controller")
    ]
    crds = ("kustomizations.kustomize.toolkit.fluxcd.io",
            "helmreleases.helm.toolkit.fluxcd.io")
    return {
        "_argocd_flux_crds": {"results": [
            {"item": name, "rc": 0,
             "stdout": "customresourcedefinition/" + name if flux or empty_flux_crds else ""}
            for name in crds
        ]},
        "_argocd_flux_kustomizations": json_probe(kustomizations if flux else []),
        "_argocd_flux_helmreleases": json_probe([]),
        "_argocd_flux_deployments": json_probe(controllers if flux else []),
        "_argocd_flux_pods": json_probe([]),
        "_argocd_flux_source": {"rc": 0, "stdout": json.dumps(source)},
        "_argocd_crd_probe": {"rc": 0, "stdout": ""},
        "_argocd_root_probe": {"rc": 0, "stdout": ""},
    }


def change_item(probes, register, mutate):
    response = json.loads(probes[register]["stdout"])
    mutate(response["items"][0])
    probes[register] = json_probe(response["items"])


def scenario(label, probes=None, failure=None, variables=None, install_needed=True,
             install=True, root=True, stage=True, checks=()):
    return {"label": label, "probes": probes if probes is not None else cluster_probes(),
            "failure": failure, "variables": variables or {},
            "install_needed": install_needed, "install": install, "root": root,
            "stage": stage, "checks": list(checks)}


class MigrationLayoutTests(unittest.TestCase):
    def test_guard_precedes_every_mutation_and_defaults_are_opt_in(self):
        defaults = load_yaml(ROLE / "defaults/main.yml")
        self.assertIs(defaults["argocd_migrate_from_flux"], False)
        self.assertIs(defaults["argocd_bootstrap_reapply_root"], False)
        role = load_yaml(ROLE / "tasks/main.yml")
        self.assertEqual(len(role), 1)
        self.assertNotIn("when", role[0])
        self.assertNotIn("rescue", role[0])
        tasks = role[0]["block"]
        guard = tasks[0]
        self.assertEqual(guard["ansible.builtin.include_tasks"], "guard.yml")
        self.assertNotIn("when", guard)
        flattened = list(walk_tasks(tasks))
        guard_index = flattened.index(guard)
        mutations = []
        for index, task in enumerate(flattened):
            self.assertFalse(task.get("ignore_errors", False))
            argv = task.get("ansible.builtin.command", {}).get("argv", [])
            if (any(module in task for module in (
                    "ansible.builtin.tempfile", "ansible.builtin.copy", "ansible.builtin.file"))
                    or isinstance(argv, list) and "apply" in argv):
                mutations.append(task)
                self.assertGreater(index, guard_index, task["name"])
        self.assertGreaterEqual(len(mutations), 5)

    def test_guard_probes_are_read_only_and_harness_rejects_commands(self):
        guard = load_yaml(ROLE / "tasks/guard.yml")
        registers = set()
        for task in walk_tasks(guard):
            if "ansible.builtin.command" in task:
                self.assertEqual(task["ansible.builtin.command"]["argv"][:3],
                                 ["/usr/local/bin/k3s", "kubectl", "get"])
                self.assertIs(task["changed_when"], False)
                self.assertIs(task["check_mode"], False)
                self.assertNotIn("failed_when", task)
                registers.add(task["register"])
        self.assertEqual(registers, GUARD_REGISTERS)
        local_actions_only(mock_probes(guard, cluster_probes(), GUARD_REGISTERS))
        with self.assertRaises(AssertionError):
            local_actions_only([{"ansible.builtin.command": {"argv": ["kubectl", "get", "pods"]}}])
        with self.assertRaises(AssertionError):
            local_actions_only([{"ansible.builtin.include_role": {"name": "argocd_bootstrap"}}])

    def test_standalone_never_imports_k3s_or_real_inventory_variables(self):
        plays = load_yaml(ROOT / "ansible/playbooks/k3s-argocd.yml")
        self.assertEqual(len(plays), 1)
        play = plays[0]
        self.assertEqual(play["hosts"], "k3s_bootstrap")
        self.assertIs(play["gather_facts"], False)
        self.assertEqual(play["roles"], ["argocd_bootstrap"])
        for key in ("tasks", "pre_tasks", "post_tasks", "vars_files", "import_playbook"):
            self.assertNotIn(key, play)
        setup = load_yaml(ROOT / "ansible/playbooks/k3s-setup.yml")
        imports = [task["ansible.builtin.import_role"]["name"]
                   for task in walk_tasks(setup[1]["tasks"])
                   if "ansible.builtin.import_role" in task]
        self.assertIn("argocd_bootstrap", imports)

    def test_bootstrap_root_is_excluded_from_its_source_to_avoid_cycles(self):
        root = load_yaml(GITOPS / "argocd/applications/root.yaml")
        source = ROOT / root["spec"]["source"]["path"]
        self.assertEqual(source, GITOPS / "argocd/applications")
        resources = load_yaml(source / "kustomization.yaml")["resources"]
        self.assertEqual(set(resources), {"argocd.yaml", "sealed-secrets.yaml", "apps.yaml"})
        self.assertNotIn("root.yaml", resources)
        children = [load_yaml(source / name) for name in resources]
        self.assertNotIn(root["metadata"]["name"], [app["metadata"]["name"] for app in children])
        for app in children:
            self.assertEqual(app["metadata"]["namespace"], "argocd")
            self.assertNotEqual(ROOT / app["spec"]["source"]["path"], source)
        role_tasks = list(walk_tasks(load_yaml(ROLE / "tasks/main.yml")))
        self.assertTrue(any(task.get("ansible.builtin.include_tasks") == "wait.yml"
                            and task.get("vars", {}).get("_argocd_wait_name") ==
                            "{{ _argocd_root.metadata.name }}" for task in role_tasks))
        self.assertTrue(any(task.get("ansible.builtin.include_tasks") == "wait.yml"
                            and task.get("loop") == "{{ _argocd_children }}" for task in role_tasks))
        waits = load_yaml(ROLE / "tasks/wait.yml")
        self.assertEqual(len(waits), 2)
        flags = [arg for task in waits for arg in task["ansible.builtin.command"]["argv"]]
        self.assertIn("--for=jsonpath={.status.sync.status}=Synced", flags)
        self.assertIn("--for=jsonpath={.status.health.status}=Healthy", flags)


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
class MigrationRuntimeTests(unittest.TestCase):
    def run_cases(self, cases, check_mode=False):
        guard = load_yaml(ROLE / "tasks/guard.yml")
        bootstrap = load_yaml(ROLE / "tasks/main.yml")[0]["block"]
        defaults = load_yaml(ROLE / "defaults/main.yml")
        root = load_yaml(GITOPS / "argocd/applications/root.yaml")
        plays = []
        for case in cases:
            reset: dict[str, Any] = {register: {"skipped": True, "changed": False}
                     for register in GUARD_REGISTERS | BOOTSTRAP_REGISTERS}
            reset.update({marker: False for marker in MARKERS})
            reset.update(_test_guard_passed=False, _test_failure_task="", _test_failure_result={},
                         _argocd_install_needed=False, _argocd_flux_items=[],
                         _argocd_flux_helm_items=[], _argocd_flux_controller_items=[])
            actual_guard = mock_probes(guard, case["probes"], GUARD_REGISTERS)
            decisions = bootstrap_decisions(bootstrap, case["probes"])
            if case["failure"]:
                assertions = [
                    "not _test_guard_passed",
                    "_test_failure_task == " + json.dumps(case["failure"]),
                    ("_test_failure_result.results | default([_test_failure_result]) "
                     "| selectattr('assertion', 'defined') "
                     "| selectattr('evaluated_to', 'defined') "
                     "| selectattr('evaluated_to', 'equalto', false) | list | length > 0"),
                ] + ["not " + marker for marker in MARKERS]
            else:
                assertions = ["_test_guard_passed", "_test_failure_task == ''"]
                for fact, expected in (
                        ("_argocd_install_needed", case["install_needed"]),
                        ("_test_stage_allocated", case["stage"]),
                        ("_test_install_applied", case["install"]),
                        ("_test_root_applied", case["root"])):
                    assertions.append(f"{fact} == {'true' if expected else 'false'}")
            tasks = [
                {"name": "Reset mock registers and decision markers",
                 "ansible.builtin.set_fact": reset},
                {"name": "Execute only real guard assertions and bootstrap conditions",
                 "block": actual_guard + [
                     {"ansible.builtin.set_fact": {"_test_guard_passed": True}}
                 ] + decisions,
                 "rescue": [{"name": "Record the real Ansible assertion failure",
                             "ansible.builtin.set_fact": {
                                 "_test_failure_task": "{{ ansible_failed_task.name }}",
                                 "_test_failure_result": "{{ ansible_failed_result }}",
                             }}]},
                {"name": "Verify offline scenario: " + case["label"],
                 "ansible.builtin.assert": {"that": assertions + case["checks"]}},
            ]
            local_actions_only(tasks)
            plays.append({
                "name": "Offline migration: " + case["label"],
                "hosts": "localhost", "connection": "local", "become": False,
                "gather_facts": False,
                "vars": {**defaults, "_argocd_root": root, **case["variables"]},
                "tasks": tasks,
            })
        with tempfile.TemporaryDirectory(prefix="argocd-migration-test-") as directory:
            temporary = Path(directory)
            inventory = temporary / "hosts.yml"
            inventory.write_text(yaml.safe_dump({"all": {"children": {"k3s_bootstrap": {
                "hosts": {"localhost": {"ansible_connection": "local",
                                        "ansible_host": "127.0.0.1",
                                        "ansible_python_interpreter": sys.executable}}
            }}}}, sort_keys=False))
            config = temporary / "ansible.cfg"
            config.write_text(
                "[defaults]\nstdout_callback = default\nretry_files_enabled = False\n"
                "callback_plugins = " + str(temporary / "callbacks") +
                "\nlocal_tmp = " + str(temporary / "ansible-tmp") +
                "\n[privilege_escalation]\nbecome = False\n"
            )
            playbook = temporary / "assertions.yml"
            playbook.write_text(yaml.safe_dump(plays, sort_keys=False))
            # Do not inherit repository inventory, Vault, callbacks, logging or privilege settings.
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("ANSIBLE_")}
            environment.update(ANSIBLE_CONFIG=str(config), ANSIBLE_NOCOLOR="1",
                               PYTHONDONTWRITEBYTECODE="1")
            command = [shutil.which("ansible-playbook"), "--inventory", str(inventory),
                       str(playbook)]
            if check_mode:
                command.append("--check")
            result = subprocess.run(command, cwd=temporary, env=environment,
                                    capture_output=True, text=True, timeout=180, check=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertRegex(output, r"failed=0\b")
            for case in cases:
                self.assertIn("TASK [Verify offline scenario: " + case["label"] + "]", output)

    def test_fresh_install_without_flux_and_empty_flux_crds(self):
        fresh = cluster_probes()
        # Unavailable CRDs must result in skipped registers, not parsing a mock stdout.
        fresh["_argocd_flux_kustomizations"]["stdout"] = "not queried: CRD absent"
        fresh["_argocd_flux_helmreleases"]["stdout"] = "not queried: CRD absent"
        fresh["_argocd_flux_source"]["stdout"] = "not queried: no Kustomizations"
        self.run_cases([
            scenario("no Flux", fresh, checks=(
                "_argocd_flux_kustomizations.skipped", "_argocd_flux_helmreleases.skipped",
                "_argocd_flux_source.skipped", "_argocd_root_probe.skipped")),
            scenario("unused Flux CRDs do not require opt-in", cluster_probes(empty_flux_crds=True)),
        ])

    def test_protected_migration_and_normalized_repository_url(self):
        probes = cluster_probes(flux=True)
        canonical = copy.deepcopy(probes)
        source = json.loads(canonical["_argocd_flux_source"]["stdout"])
        source["spec"]["url"] += ".git"
        canonical["_argocd_flux_source"]["stdout"] = json.dumps(source)
        change_item(canonical, "_argocd_flux_kustomizations",
                    lambda item: item["spec"]["sourceRef"].update(namespace="flux-system"))
        self.run_cases([
            scenario("protected Flux handover", probes,
                     variables={"argocd_migrate_from_flux": True},
                     checks=("_argocd_flux_items | length == 3",
                             "_argocd_flux_controller_items | length == 4")),
            scenario("dot-git URL and string opt-in", canonical,
                     variables={"argocd_migrate_from_flux": "true"}),
        ])

    def test_existing_flux_requires_explicit_opt_in(self):
        self.run_cases([
            scenario("missing migration opt-in", cluster_probes(flux=True), failure=OPT_IN),
            scenario("string false does not enable migration", cluster_probes(flux=True),
                     failure=OPT_IN, variables={"argocd_migrate_from_flux": "false"}),
        ])

    def test_active_flux_and_unsafe_prune_or_deletion_policy_are_rejected(self):
        cases = []
        for field, value in (("suspend", False), ("prune", True),
                             ("deletionPolicy", "MirrorPrune"), ("deletionPolicy", "Delete")):
            probes = cluster_probes(flux=True)
            change_item(probes, "_argocd_flux_kustomizations",
                        lambda item, field=field, value=value: item["spec"].update({field: value}))
            cases.append(scenario(f"unsafe {field}={value}", probes, failure=KUSTOMIZATION_GUARD,
                                  variables={"argocd_migrate_from_flux": True}))
        for field in ("suspend", "deletionPolicy"):
            probes = cluster_probes(flux=True)
            change_item(probes, "_argocd_flux_kustomizations", lambda item, field=field: item["spec"].pop(field))
            cases.append(scenario("missing " + field, probes, failure=KUSTOMIZATION_GUARD,
                                  variables={"argocd_migrate_from_flux": True}))
        self.run_cases(cases)

    def test_unsupported_kustomizations_and_helmreleases_are_rejected(self):
        mutations = [
            ("unknown namespace", lambda item: item["metadata"].update(namespace="other")),
            ("unknown name", lambda item: item["metadata"].update(name="unaccounted-workloads")),
            ("different path", lambda item: item["spec"].update(path="./other")),
            ("non-Git source", lambda item: item["spec"]["sourceRef"].update(kind="OCIRepository")),
            ("different source name", lambda item: item["spec"]["sourceRef"].update(name="other")),
            ("different source namespace", lambda item: item["spec"]["sourceRef"].update(namespace="other")),
            ("deletion in progress", lambda item: item["metadata"].update(deletionTimestamp="2026-10-11T00:00:00Z")),
        ]
        cases = []
        for label, mutate in mutations:
            probes = cluster_probes(flux=True)
            change_item(probes, "_argocd_flux_kustomizations", mutate)
            cases.append(scenario(label, probes, failure=KUSTOMIZATION_GUARD,
                                  variables={"argocd_migrate_from_flux": True}))
        for field, value in (("kubeConfig", {}), ("postBuild", {}), ("decryption", {}),
                             ("patches", []), ("components", []), ("targetNamespace", "default"),
                             ("namePrefix", ""), ("nameSuffix", "")):
            probes = cluster_probes(flux=True)
            change_item(probes, "_argocd_flux_kustomizations",
                        lambda item, field=field, value=value: item["spec"].update({field: value}))
            cases.append(scenario("customization present: " + field, probes,
                                  failure=KUSTOMIZATION_GUARD,
                                  variables={"argocd_migrate_from_flux": True}))
        helm = cluster_probes(flux=True)
        helm["_argocd_flux_helmreleases"] = json_probe([
            {"metadata": {"namespace": "default", "name": "unconverted-chart"},
             "spec": {"suspend": True}}
        ])
        cases.append(scenario("even suspended HelmReleases need conversion", helm,
                              failure=HELM_GUARD, variables={"argocd_migrate_from_flux": True}))
        self.run_cases(cases)

    def test_unrelated_or_transformed_source_is_rejected(self):
        cases = []
        mutations = [
            ("different repository", lambda spec: spec.update(url="https://github.com/example/other")),
            ("different branch", lambda spec: spec["ref"].update(branch="other")),
            ("missing branch", lambda spec: spec["ref"].pop("branch")),
            ("commit pin", lambda spec: spec["ref"].update(commit="a" * 40)),
            ("tag pin", lambda spec: spec["ref"].update(tag="main")),
            ("semver pin", lambda spec: spec["ref"].update(semver="*")),
            ("include even when empty", lambda spec: spec.update(include=[])),
        ]
        for label, mutate in mutations:
            probes = cluster_probes(flux=True)
            source = json.loads(probes["_argocd_flux_source"]["stdout"])
            mutate(source["spec"])
            probes["_argocd_flux_source"]["stdout"] = json.dumps(source)
            cases.append(scenario(label, probes, failure=SOURCE_GUARD,
                                  variables={"argocd_migrate_from_flux": True}))
        self.run_cases(cases)

    def test_controller_replicas_and_remaining_pods_are_rejected(self):
        cases = []
        for key in ("spec", "status"):
            probes = cluster_probes(flux=True)
            change_item(probes, "_argocd_flux_deployments",
                        lambda item, key=key: item[key].update(replicas=1))
            cases.append(scenario("controller replicas in " + key, probes,
                                  failure=CONTROLLER_GUARD,
                                  variables={"argocd_migrate_from_flux": True}))
        missing = cluster_probes(flux=True)
        change_item(missing, "_argocd_flux_deployments", lambda item: item["spec"].pop("replicas"))
        cases.append(scenario("omitted desired replicas defaults to one", missing,
                              failure=CONTROLLER_GUARD,
                              variables={"argocd_migrate_from_flux": True}))
        for label, status, timestamp in (("running", "Running", None),
                                         ("terminating", "Running", "2026-10-11T00:00:00Z"),
                                         ("completed", "Succeeded", None)):
            probes = cluster_probes(flux=True)
            pod = {"metadata": {"namespace": "flux-system", "name": "controller-pod"},
                   "status": {"phase": status}}
            if timestamp:
                pod["metadata"]["deletionTimestamp"] = timestamp
            probes["_argocd_flux_pods"] = json_probe([pod])
            cases.append(scenario(label + " controller Pod remains", probes, failure=POD_GUARD,
                                  variables={"argocd_migrate_from_flux": True}))
        self.run_cases(cases)

    def test_existing_root_is_skipped_and_explicit_reapply_only_updates_root(self):
        cases = []
        for crd_exists, root_exists, reapply in (
                (False, False, False), (True, False, False), (True, True, False),
                (True, True, True), (False, False, True)):
            probes = cluster_probes()
            probes["_argocd_crd_probe"]["stdout"] = "crd/applications.argoproj.io" if crd_exists else ""
            probes["_argocd_root_probe"]["stdout"] = "application/local-root" if root_exists else ""
            install_needed = not root_exists
            cases.append(scenario(f"crd={crd_exists}, root={root_exists}, reapply={reapply}", probes,
                                  variables={"argocd_bootstrap_reapply_root": reapply},
                                  install_needed=install_needed, install=install_needed,
                                  root=install_needed or reapply, stage=install_needed or reapply))
        # A missing Application CRD means the root probe is skipped, even with stale mock output.
        stale = cluster_probes()
        stale["_argocd_root_probe"]["stdout"] = "must not be used: CRD absent"
        cases.append(scenario("skipped root probe cannot suppress bootstrap", stale,
                              checks=("_argocd_root_probe.skipped",)))
        active = cluster_probes(flux=True)
        active["_argocd_crd_probe"]["stdout"] = "crd/applications.argoproj.io"
        active["_argocd_root_probe"]["stdout"] = "application/local-root"
        cases.append(scenario("existing root does not bypass the Flux guard", active, failure=OPT_IN))
        self.run_cases(cases)

    def test_check_mode_runs_guard_but_never_stages_or_applies(self):
        existing = cluster_probes()
        existing["_argocd_crd_probe"]["stdout"] = "crd/applications.argoproj.io"
        existing["_argocd_root_probe"]["stdout"] = "application/local-root"
        self.run_cases([
            scenario("fresh install in check mode", install=False, root=False, stage=False),
            scenario("root reapply in check mode", existing,
                     variables={"argocd_bootstrap_reapply_root": True},
                     install_needed=False, install=False, root=False, stage=False),
            scenario("active Flux still rejected in check mode", cluster_probes(flux=True),
                     failure=OPT_IN),
        ], check_mode=True)


if __name__ == "__main__":
    unittest.main()
