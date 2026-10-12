"""Offline UI regressions; kubectl is used only for local Kustomize builds."""

import hashlib
import json
import shutil
import unittest
from collections import Counter
from itertools import pairwise
from pathlib import Path

from test_postgres_operator import (
    COMMIT,
    OPERATOR,
    REPOSITORY,
    SYNC_WAVE,
    TAG,
    documents,
    kustomize,
    load_yaml,
    walk_values,
)

UI = OPERATOR / "ui"
NAME = "postgres-operator-ui"
VENDOR_FILES = {
    "deployment.yaml": (
        "1481cbcec277093b1cb2fcc043540ef5e58d615f9b558eca509a2f1a96d453e2", 3488,
    ),
    "service.yaml": (
        "deef99ab877db6ff917197dbd1c71b9dc9fda3470cfc5a551670996651ee5e6f", 287,
    ),
}
VIEWER_PERMISSIONS = {
    ("acid.zalan.do", "postgresqls"): {"get", "list"},
    ("", "pods"): {"list"},
    ("", "services"): {"get"},
    ("apps", "statefulsets"): {"get"},
    ("apps", "deployments"): {"get"},
}
UI_NAMESPACES = {
    "ServiceAccount": "postgres-operator",
    "Role": "databases",
    "RoleBinding": "databases",
    "Deployment": "postgres-operator",
    "Service": "postgres-operator",
}


class UIVendorTests(unittest.TestCase):
    def test_release_matches_operator_and_pristine_upstream_manifest_bytes(self):
        release = json.loads((UI / "release.json").read_text())
        operator_release = json.loads((OPERATOR / "release.json").read_text())
        for key, expected in (
            ("repository", REPOSITORY), ("tag", TAG), ("commit", COMMIT),
            ("release_url", REPOSITORY + "/releases/tag/" + TAG),
        ):
            self.assertEqual(release[key], expected, key)
            self.assertEqual(release[key], operator_release[key], key)
        files = release["files"]
        self.assertEqual(len(files), 2)
        self.assertEqual({entry["manifest"] for entry in files},
                         {"vendor/" + name for name in VENDOR_FILES})
        for entry in files:
            with self.subTest(manifest=entry["manifest"]):
                name = Path(entry["manifest"]).name
                digest, size = VENDOR_FILES[name]
                source = ("https://raw.githubusercontent.com/zalando/postgres-operator/"
                          + COMMIT + "/ui/manifests/" + name)
                self.assertEqual(entry["immutable_source"], source)
                self.assertEqual(entry["sha256"], digest)
                self.assertEqual(entry["size_bytes"], size)
                content = (UI / entry["manifest"]).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
                self.assertEqual(len(content), size)
        self.assertEqual({path.name for path in (UI / "vendor").iterdir()},
                         set(VENDOR_FILES) | {"kustomization.yaml"})

    def test_vendor_base_contains_only_the_two_local_frontend_manifests(self):
        base = load_yaml(UI / "vendor/kustomization.yaml")
        self.assertEqual(set(base), {"apiVersion", "kind", "resources"})
        self.assertEqual(base["apiVersion"], "kustomize.config.k8s.io/v1beta1")
        self.assertEqual(base["kind"], "Kustomization")
        self.assertEqual(Counter(base["resources"]), Counter(VENDOR_FILES.keys()))
        vendored = [doc for name in base["resources"]
                    for doc in documents(UI / "vendor" / name)]
        self.assertEqual(Counter(doc["kind"] for doc in vendored),
                         Counter({"Deployment": 1, "Service": 1}))


class UISourceTests(unittest.TestCase):
    def test_cross_namespace_rbac_stays_outside_the_frontend_overlay(self):
        root = load_yaml(UI / "kustomization.yaml")
        self.assertEqual(set(root), {"apiVersion", "kind", "resources"})
        self.assertEqual(root["resources"], ["rbac.yaml", "frontend"])
        frontend = load_yaml(UI / "frontend/kustomization.yaml")
        self.assertEqual(set(frontend),
                         {"apiVersion", "kind", "namespace", "resources", "patches"})
        self.assertEqual(frontend["namespace"], "postgres-operator")
        self.assertEqual(frontend["resources"], ["../vendor"])
        self.assertEqual([patch["path"] for patch in frontend["patches"] if "path" in patch],
                         ["deployment.yaml"])
        rbac = documents(UI / "rbac.yaml")
        self.assertEqual(len(rbac), 3)
        self.assertEqual({doc["kind"] for doc in rbac},
                         {"ServiceAccount", "Role", "RoleBinding"})
        for doc in rbac:
            with self.subTest(kind=doc["kind"]):
                self.assertEqual(doc["metadata"]["name"], NAME)
                self.assertEqual(doc["metadata"]["namespace"], UI_NAMESPACES[doc["kind"]])
                self.assertEqual(doc["metadata"]["annotations"][SYNC_WAVE], "5")

    def test_role_grants_only_the_pinned_ui_viewing_operations(self):
        role = next(doc for doc in documents(UI / "rbac.yaml") if doc["kind"] == "Role")
        permissions = {}
        for rule in role["rules"]:
            # Exact fields also exclude non-resource URLs and unexpected RBAC paths.
            self.assertEqual(set(rule), {"apiGroups", "resources", "verbs"})
            for key in ("apiGroups", "resources", "verbs"):
                self.assertTrue(rule[key], key)
            for group in rule["apiGroups"]:
                for resource in rule["resources"]:
                    permissions.setdefault((group, resource), set()).update(rule["verbs"])
        # No wildcards, Secrets, subresources (including pods/exec), watch, or writes.
        self.assertEqual(permissions, VIEWER_PERMISSIONS)


@unittest.skipUnless(shutil.which("kubectl"), "kubectl is not installed; local build checks skipped")
class UIRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = kustomize(UI)
        cls.resources = {doc["kind"]: doc for doc in cls.rendered}
        cls.parent = kustomize(OPERATOR)
        cls.deployment = cls.resources["Deployment"]
        cls.pod = cls.deployment["spec"]["template"]["spec"]
        cls.container = cls.pod["containers"][0]
        cls.upstream_container = load_yaml(UI / "vendor/deployment.yaml")[
            "spec"]["template"]["spec"]["containers"][0]

    def test_ui_renders_exactly_five_namespace_scoped_resources(self):
        self.assertEqual(len(self.rendered), 5)
        self.assertEqual(set(self.resources), set(UI_NAMESPACES))
        for doc in self.rendered:
            with self.subTest(kind=doc["kind"]):
                self.assertEqual(doc["metadata"]["name"], NAME)
                self.assertEqual(doc["metadata"]["namespace"], UI_NAMESPACES[doc["kind"]])
        # The exact inventory excludes Ingress, cluster RBAC, Secrets, PVCs, and databases.
        self.assertEqual(self.deployment["spec"]["replicas"], 1)
        self.assertEqual(len(self.pod["containers"]), 1)
        self.assertEqual(self.container["name"], "service")
        self.assertFalse(self.pod.get("initContainers"))

    def test_parent_preserves_ui_namespaces_and_only_its_viewer_role_binding(self):
        self.assertEqual(len(self.parent), 16)
        parent_resources = {
            (doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"]): doc
            for doc in self.parent
        }
        for doc in self.rendered:
            identity = (doc["kind"], doc["metadata"]["namespace"], NAME)
            with self.subTest(kind=doc["kind"]):
                # Avoid dumping a rendered resource if a regression introduces credentials.
                self.assertTrue(parent_resources[identity] == doc, "UI changed in parent build")
        binding = self.resources["RoleBinding"]
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": NAME,
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": NAME, "namespace": "postgres-operator",
        }])
        bindings = [doc for doc in self.parent
                    if doc["kind"] in {"RoleBinding", "ClusterRoleBinding"}
                    and any(subject.get("kind") == "ServiceAccount"
                            and subject.get("name") == NAME
                            for subject in doc.get("subjects", []))]
        self.assertTrue(bindings == [binding], "UI must not inherit another RBAC binding")
        role = next(doc for doc in documents(UI / "rbac.yaml") if doc["kind"] == "Role")
        self.assertEqual(self.resources["Role"]["rules"], role["rules"])
        self.assertEqual(self.pod["serviceAccountName"], NAME)
        self.assertIsNot(self.pod.get("automountServiceAccountToken"), False)
        self.assertIsNot(self.resources["ServiceAccount"].get("automountServiceAccountToken"), False)

    def test_readonly_backend_and_frontend_configuration_agree(self):
        entries = self.container["env"]
        env = {item["name"]: item["value"] for item in entries}
        self.assertEqual(len(entries), len(env), "duplicate environment variable")
        expected = {
            "APP_URL": "http://localhost:8081",
            "OPERATOR_API_URL": "http://postgres-operator.postgres-operator.svc.cluster.local:8080",
            "OPERATOR_CLUSTER_NAME_LABEL": "cluster-name",
            "TARGET_NAMESPACE": "databases",
            "READ_ONLY_MODE": "true",
            "TEAMS": "[]",
            "SUPERUSER_TEAM": "",
        }
        self.assertEqual(set(env), set(expected) | {"OPERATOR_UI_CONFIG"})
        for item in entries:
            self.assertEqual(set(item), {"name", "value"}, item["name"])
            self.assertIsInstance(item["value"], str, item["name"])
        for name, value in expected.items():
            self.assertTrue(env[name] == value, name)
        teams = json.loads(env["TEAMS"])
        config = json.loads(env["OPERATOR_UI_CONFIG"])
        self.assertEqual(teams, [])
        self.assertIs(config.get("read_only_mode", env["READ_ONLY_MODE"] == "true"), True)
        self.assertEqual(config.get("superuser_team", env["SUPERUSER_TEAM"]), "")
        self.assertNotIn(config.get("superuser_team", env["SUPERUSER_TEAM"]), teams)
        self.assertEqual(config.get("target_namespace", env["TARGET_NAMESPACE"]), "databases")
        operator_config = next(doc["configuration"] for doc in self.parent
                               if doc["kind"] == "OperatorConfiguration")
        self.assertEqual(env["TARGET_NAMESPACE"], operator_config["kubernetes"]["watched_namespace"])
        self.assertEqual(env["OPERATOR_CLUSTER_NAME_LABEL"],
                         operator_config["kubernetes"]["cluster_name_label"])

    def test_ui_config_hides_load_balancers_and_uses_json_booleans(self):
        env = {item["name"]: item["value"] for item in self.container["env"]}
        config = json.loads(env["OPERATOR_UI_CONFIG"])
        for key in ("master_load_balancer_visible", "replica_load_balancer_visible",
                    "nat_gateways_visible"):
            self.assertIs(config[key], False, key)
        for key, value in config.items():
            if "load_balancer" in key and key.endswith("_visible"):
                self.assertIs(value, False, key)
        for key in ("databases_visible", "resources_visible", "users_visible"):
            self.assertIs(config[key], True, key)
        self.assertEqual(config["postgresql_versions"], ["18"])
        self.assertNotIn("RESOURCES_VISIBLE", env)

    def test_ui_has_no_credentials_external_services_or_debug_configuration(self):
        forbidden_keys = {
            "secret", "secretname", "secretref", "secretkeyref", "imagepullsecrets",
            "credentials", "envfrom",
        }
        for doc in self.rendered:
            for path, value in walk_values(doc):
                key = path.rsplit(".", 1)[-1].lower()
                self.assertTrue(key not in forbidden_keys and "password" not in key,
                                f"{doc['kind']} {path}: credentials must not be configured")
        args = self.container.get("command", []) + self.container.get("args", [])
        self.assertNotIn("--debug", args)
        self.assertNotIn("-d", args)
        config = json.loads(next(item["value"] for item in self.container["env"]
                                 if item["name"] == "OPERATOR_UI_CONFIG"))
        self.assertFalse(config.get("kubernetes_maintenance_check"))

    def test_service_and_health_probe_match_localhost_8081_to_service_80(self):
        service = self.resources["Service"]["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertEqual(service["ports"], [{"port": 80, "targetPort": 8081, "protocol": "TCP"}])
        for key in ("externalIPs", "externalName", "loadBalancerIP", "loadBalancerClass"):
            self.assertNotIn(key, service)
        labels = self.deployment["spec"]["template"]["metadata"]["labels"]
        self.assertTrue(set(service["selector"].items()) <= set(labels.items()))
        self.assertTrue(set(self.deployment["spec"]["selector"]["matchLabels"].items()) <=
                        set(labels.items()))
        self.assertEqual(self.container["ports"], [{"containerPort": 8081, "protocol": "TCP"}])
        probe = self.container["readinessProbe"]
        self.assertEqual(probe, self.upstream_container["readinessProbe"])
        self.assertEqual(probe["httpGet"], {"path": "/health", "port": 8081})
        if "livenessProbe" in self.container:
            self.assertEqual(self.container["livenessProbe"]["httpGet"], probe["httpGet"])

    def test_pinned_image_resources_and_nonroot_security_context(self):
        self.assertEqual(self.container["image"], "ghcr.io/zalando/postgres-operator-ui:" + TAG)
        self.assertEqual(self.container["resources"], self.upstream_container["resources"])
        pod_context = self.pod["securityContext"]
        self.assertIs(pod_context["runAsNonRoot"], True)
        self.assertEqual(pod_context["runAsUser"], 1000)
        self.assertEqual(pod_context["runAsGroup"], 1000)
        self.assertEqual(pod_context["seccompProfile"], {"type": "RuntimeDefault"})
        context = self.container["securityContext"]
        self.assertIs(context["allowPrivilegeEscalation"], False)
        self.assertEqual(context["capabilities"], {"drop": ["ALL"]})
        self.assertFalse(context.get("privileged", False))
        for key in ("runAsNonRoot", "runAsUser", "runAsGroup", "seccompProfile"):
            self.assertEqual(context.get(key, pod_context[key]), pod_context[key], key)
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            self.assertFalse(self.pod.get(key, False), key)

    def test_operator_precedes_ui_rbac_service_and_deployment_waves(self):
        for doc in self.rendered:
            with self.subTest(kind=doc["kind"]):
                self.assertEqual(doc["metadata"]["annotations"][SYNC_WAVE],
                                 "10" if doc["kind"] == "Deployment" else "5")
        config = next(doc for doc in self.parent if doc["kind"] == "OperatorConfiguration")
        operator = next(doc for doc in self.parent
                        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "postgres-operator")
        waves = [int(doc["metadata"]["annotations"][SYNC_WAVE])
                 for doc in (config, operator, self.resources["Role"], self.deployment)]
        self.assertEqual(waves, [-5, 0, 5, 10])
        self.assertTrue(all(before < after for before, after in pairwise(waves)))
