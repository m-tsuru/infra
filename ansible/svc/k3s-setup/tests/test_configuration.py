"""Local-only configuration checks; never connect to the inventory hosts."""

import base64
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[2]
GITOPS = SERVICE / "gitops"
INVENTORY = SERVICE / "inventory/hosts.yml"
K3S_ROLE = SERVICE / "roles/k3s"


def load_yaml(path):
    return yaml.safe_load(path.read_text())


def load_documents(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc is not None]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.inventory = load_yaml(INVENTORY)
        self.cluster = self.inventory["all"]["children"]["k3s_cluster"]["children"]
        self.servers = self.cluster["k3s_servers"]["hosts"]
        self.defaults = load_yaml(K3S_ROLE / "defaults/main.yml")
        self.tasks = load_yaml(K3S_ROLE / "tasks/main.yml")

    def test_inventory_and_serial_bootstrap(self):
        self.assertEqual(
            {host: values["ansible_host"] for host, values in self.servers.items()},
            {
                "sakuta.local.sasakulab.com": "192.168.37.110",
                "kaede.local.sasakulab.com": "192.168.37.112",
                "nodoka.local.sasakulab.com": "192.168.37.113",
            },
        )
        self.assertEqual(
            list(self.cluster["k3s_bootstrap"]["hosts"]), [next(iter(self.servers))]
        )
        plays = load_yaml(ROOT / "ansible/playbooks/k3s-setup.yml")
        self.assertEqual(plays[0]["hosts"], "k3s_servers")
        self.assertEqual(plays[0]["serial"], 1)
        self.assertEqual(plays[0]["order"], "inventory")
        self.assertTrue(plays[0]["any_errors_fatal"])
        self.assertEqual(plays[1]["hosts"], "k3s_bootstrap")
        self.assertEqual(plays[1]["tasks"][-1]["ansible.builtin.import_role"]["name"],
                         "argocd_bootstrap")

    def test_token_is_protected_and_download_is_verified(self):
        token_task = next(
            task for task in self.tasks
            if task.get("ansible.builtin.copy", {}).get("dest") == "/etc/rancher/k3s/token"
        )
        self.assertTrue(token_task["no_log"])
        self.assertFalse(token_task["diff"])
        self.assertEqual(token_task["ansible.builtin.copy"]["mode"], "0600")
        self.assertIn("_k3s_effective_token", token_task["ansible.builtin.copy"]["content"])
        token_tasks = load_yaml(K3S_ROLE / "tasks/token.yml")
        slurp = next(task for task in token_tasks if "ansible.builtin.slurp" in task)
        self.assertTrue(slurp["no_log"])
        self.assertEqual(slurp["delegate_to"], "{{ k3s_bootstrap_host }}")
        download = next(task["ansible.builtin.get_url"] for task in self.tasks
                        if "ansible.builtin.get_url" in task)
        self.assertTrue(download["checksum"].startswith("sha256:"))
        self.assertIn("sha256sum-", download["checksum"])
        self.assertRegex(self.defaults["k3s_version"], r"^v\d+\.\d+\.\d+\+k3s\d+$")

    def test_restart_guard_and_successful_revision_recording(self):
        restart = load_yaml(K3S_ROLE / "tasks/restart.yml")
        start_index = next(i for i, task in enumerate(restart)
                           if "ansible.builtin.systemd_service" in task)
        guard_index = next(i for i, task in enumerate(restart)
                           if task.get("delegate_to") == "{{ item }}")
        self.assertLess(guard_index, start_index)
        self.assertIn("--raw=/readyz", restart[guard_index]["ansible.builtin.command"]["argv"])
        self.assertIn("_k3s_restart_required", restart[guard_index]["when"])
        marker_index = next(i for i, task in enumerate(self.tasks)
                            if task.get("ansible.builtin.copy", {}).get("dest") ==
                            "/etc/rancher/k3s/.ansible-applied-revision")
        ready_index = next(i for i, task in enumerate(self.tasks)
                           if task.get("register") == "_k3s_node_ready")
        self.assertGreater(marker_index, ready_index)
        self.assertEqual(self.tasks[marker_index]["ansible.builtin.copy"]["mode"], "0600")

    def test_vendor_integrity(self):
        assets = {
            "clusters/local/flux-system/gotk-components.yaml":
                "9c1fda7e401429531ed1478f67ba06b3edff6513b75da7ee47bde9f1c4d4251c",
            "infrastructure/sealed-secrets/controller.yaml":
                "ac8aabccdd9110430d117501705818115b2ab0993700a9c118d4d42cf4015a17",
        }
        argo_release = json.loads((GITOPS / "argocd/release.json").read_text())
        assets["argocd/" + argo_release["manifest"]] = argo_release["sha256"]
        self.assertEqual(argo_release["tag"], "v3.5.4")
        self.assertEqual(argo_release["commit"], "d6d5b248ce00e1a2c512068002a93d3319767087")
        for relative, expected in assets.items():
            with self.subTest(asset=relative):
                self.assertEqual(hashlib.sha256((GITOPS / relative).read_bytes()).hexdigest(),
                                 expected)

    def test_gitops_local_references_and_dependency_order(self):
        for path in GITOPS.rglob("kustomization.yaml"):
            for resource in load_yaml(path).get("resources", []):
                self.assertNotIn("://", resource, str(path))
                self.assertTrue((path.parent / resource).exists(), str(path))
        documents = [doc for path in GITOPS.rglob("*.yaml") for doc in load_documents(path)]
        reconciliations = {
            doc["metadata"]["name"]: doc
            for doc in documents
            if doc.get("kind") == "Kustomization"
            and doc.get("apiVersion", "").startswith("kustomize.toolkit.fluxcd.io/")
        }
        self.assertEqual(set(reconciliations), {"flux-system", "sealed-secrets", "apps"})
        for name, doc in reconciliations.items():
            self.assertTrue(doc["spec"]["prune"])
            self.assertTrue((ROOT / doc["spec"]["path"]).is_dir(), name)
        self.assertFalse(reconciliations["flux-system"]["spec"].get("wait", False))
        self.assertTrue(reconciliations["sealed-secrets"]["spec"]["wait"])
        self.assertEqual(reconciliations["apps"]["spec"]["dependsOn"],
                         [{"name": "sealed-secrets"}])
        source = next(doc for doc in documents if doc.get("kind") == "GitRepository")
        self.assertTrue(source["spec"]["url"].startswith("https://"))
        self.assertNotIn("secretRef", source["spec"])

    def test_no_plaintext_kubernetes_secrets_or_vault_files(self):
        for path in GITOPS.rglob("*.yaml"):
            for doc in load_documents(path):
                if doc.get("kind") == "Secret":
                    # Only upstream empty placeholders are allowed. Credential
                    # values must be generated/provided at runtime, not committed.
                    self.assertEqual(path, GITOPS / "argocd/install/install.yaml")
                    self.assertIn(doc["metadata"]["name"],
                                  {"argocd-secret", "argocd-notifications-secret"})
                    self.assertFalse(doc.get("data"))
                    self.assertFalse(doc.get("stringData"))
        for path in (SERVICE / "inventory/group_vars").rglob("vault.yml"):
            self.assertTrue(path.read_text().startswith("$ANSIBLE_VAULT;"),
                            f"Plaintext Vault file: {path}")

    @unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
    def test_actual_ansible_rendering_tokens_and_failed_run_recovery_locally(self):
        # Execute only assertions and lookups against a temporary local inventory.
        # Never import the complete role or load the operator's encrypted Vault.
        token_tasks = load_yaml(K3S_ROLE / "tasks/token.yml")
        restart_tasks = load_yaml(K3S_ROLE / "tasks/restart.yml")
        expected_sans = list(self.servers) + [v["ansible_host"] for v in self.servers.values()]
        tasks = self.tasks[:3] + token_tasks[3:] + [
            {
                "name": "Check CA-bound token selection",
                "ansible.builtin.assert": {
                    "that": [
                        ("_k3s_effective_token == (vault_k3s_token if "
                         "inventory_hostname == k3s_bootstrap_host else _test_secure_token)"),
                    ]
                },
            },
            {
                "name": "Render actual k3s template with Ansible",
                "ansible.builtin.set_fact": {
                    "_test_config": "{{ lookup('ansible.builtin.template', '"
                    + str(K3S_ROLE / "templates/config.yaml.j2")
                    + "') | ansible.builtin.from_yaml }}"
                },
            },
            {
                "name": "Check rendered configuration on every server",
                "ansible.builtin.assert": {
                    "that": [
                        "_test_config['node-name'] == inventory_hostname",
                        "_test_config['node-ip'] == ansible_host",
                        "_test_config['tls-san'] | sort == _test_sans | sort",
                        "_test_config['write-kubeconfig-mode'] == '0600'",
                        "_test_config['token-file'] == '/etc/rancher/k3s/token'",
                        "'token' not in _test_config",
                        "_test_config['secrets-encryption']",
                        "_test_config['disable'] == ['traefik', 'servicelb']",
                        ("_test_config.get('cluster-init', false) == "
                         "(inventory_hostname == k3s_bootstrap_host)"),
                        "('server' not in _test_config) == (inventory_hostname == k3s_bootstrap_host)",
                        "_test_config.get('server', k3s_api_endpoint) == k3s_api_endpoint",
                    ]
                },
            },
            restart_tasks[0],
            {
                "name": "Mock the recorded healthy revision",
                "ansible.builtin.set_fact": {
                    "_k3s_applied_revision": {"content": "{{ _k3s_desired_revision | b64encode }}"}
                },
            },
            restart_tasks[3],
            {"ansible.builtin.assert": {"that": "not _k3s_restart_required"}},
            {
                "name": "Mock an interrupted prior apply",
                "ansible.builtin.set_fact": {
                    "_k3s_applied_revision": {"content": "{{ 'old-revision' | b64encode }}"}
                },
            },
            restart_tasks[3],
            {"ansible.builtin.assert": {"that": "_k3s_restart_required"}},
            next(task for task in restart_tasks
                 if task['name'] == 'Refuse to restart a two-member etcd cluster'),
        ]
        password = "local-test-value-not-an-actual-cluster-token"
        secure_token = "K10" + "a" * 64 + "::server:" + password
        for architecture in ["x86_64", "aarch64"]:
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                inventory = temporary / "hosts.yml"
                inventory.write_text(yaml.safe_dump(self.inventory, sort_keys=False))
                variables = dict(self.defaults)
                variables.update(
                    vault_k3s_token=password,
                    role_path=str(K3S_ROLE),
                    _test_sans=expected_sans,
                    _test_secure_token=secure_token,
                    _k3s_server_token_stat={"stat": {"exists": True}},
                    _k3s_server_token={"content": base64.b64encode(secure_token.encode()).decode()},
                    _k3s_revision_stat={"stat": {"exists": True}},
                    _k3s_existing_member={"stat": {"exists": True}},
                    _k3s_service_running=True,
                    _k3s_existing_servers={"stdout": json.dumps({"items": [{}, {}, {}]})},
                    _k3s_download={"changed": False},
                    _k3s_config={"changed": False},
                    _k3s_token_file={"changed": False},
                    _k3s_unit={"changed": False},
                    ansible_facts={"os_family": "Debian", "distribution": "Ubuntu",
                                   "service_mgr": "systemd", "architecture": architecture},
                )
                playbook = temporary / "render.yml"
                playbook.write_text(yaml.safe_dump([
                    {"hosts": "k3s_servers", "connection": "local", "become": False,
                     "gather_facts": False, "vars": variables, "tasks": tasks}
                ], sort_keys=False))
                result = subprocess.run(
                    ["ansible-playbook", "--inventory", str(inventory), str(playbook)],
                    cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
    def test_unsafe_tokens_and_two_member_restarts_are_rejected(self):
        restart_tasks = load_yaml(K3S_ROLE / "tasks/restart.yml")
        guard = next(task for task in restart_tasks
                     if task["name"] == "Refuse to restart a two-member etcd cluster")
        service = next(task["ansible.builtin.systemd_service"] for task in restart_tasks
                       if "ansible.builtin.systemd_service" in task)
        token_guard = load_yaml(K3S_ROLE / "tasks/token.yml")[3]
        password = "local-only-mismatched-password"
        secure_token = "K10" + "a" * 64 + "::server:another-test-password"
        common = {
            "_k3s_existing_member": {"stat": {"exists": True}},
            "_k3s_restart_required": True,
            "_k3s_service_running": True,
            "_k3s_existing_servers": {"stdout": json.dumps({"items": [{}, {}]})},
        }
        cases = [
            ("two-member-running", [guard], common, False),
            ("inactive-member-start", [guard,
                {"ansible.builtin.set_fact": {"_test_state": service["state"]}},
                {"ansible.builtin.assert": {"that": "_test_state == 'started'"}},
             ], {**common, "_k3s_service_running": False}, True),
            ("mismatched-token", [token_guard], {
                "vault_k3s_token": password,
                "_k3s_server_token_stat": {"stat": {"exists": True}},
                "_k3s_server_token": {"content": base64.b64encode(secure_token.encode()).decode()},
             }, False),
        ]
        for label, tasks, variables, succeeds in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                playbook = Path(directory) / "guards.yml"
                playbook.write_text(yaml.safe_dump([
                    {"hosts": "localhost", "connection": "local", "become": False,
                     "gather_facts": False, "vars": variables, "tasks": tasks}
                ], sort_keys=False))
                result = subprocess.run(
                    ["ansible-playbook", "--inventory", "localhost,", str(playbook)],
                    cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
                )
                self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
                self.assertNotIn(password, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
