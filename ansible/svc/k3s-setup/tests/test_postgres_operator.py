"""Offline operator regressions; kubectl is used only for local Kustomize builds."""

import copy
import hashlib
import json
import re
import shutil
import subprocess
import unittest
from collections import Counter
from pathlib import Path

import yaml

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[2]
GITOPS = SERVICE / "gitops"
OPERATOR = GITOPS / "infrastructure/postgres-operator"
APPLICATIONS = GITOPS / "argocd/applications"
TAG = "v2.0.3"
COMMIT = "80b7a074ada5221dc5eeac487c19faf810eb9cfe"
REPOSITORY = "https://github.com/zalando/postgres-operator"
SYNC_WAVE = "argocd.argoproj.io/sync-wave"
SYNC_OPTIONS = "argocd.argoproj.io/sync-options"
PROTECTION = {SYNC_WAVE: "-20", SYNC_OPTIONS: "Prune=false,Delete=false"}
VENDOR_FILES = {
    "postgresql.crd.yaml": (
        "dbe5b8b548bd9570fdcd094ba7636d306a6d41e1dbd0954bedcba788d396473d", 189200,
    ),
    "operatorconfiguration.crd.yaml": (
        "54f2d79527f0712bea0c769036f199121c9db44308eda11954a652229081c7a5", 132473,
    ),
    "postgresql-operator-default-configuration.yaml": (
        "c373972d0c0aad9c55d76786b86013f0578731a4d691d8f4723d36dca904125c", 7957,
    ),
    "operator-service-account-rbac.yaml": (
        "e2afc6cb25d38bcd3720ba7469d0f917deaec2189eedb85275e1b2024dfa650c", 5135,
    ),
    "postgres-operator.yaml": (
        "b54d63e178d7d2c15a6bf1c2057f0a6fe79f8a59f54f103b6f80e2a12482cf1f", 1350,
    ),
    "api-service.yaml": (
        "1952b75ab0f18994eaca061161ee612e52cef8fde88d06558ea54b55d5b7fac1", 192,
    ),
}
CHILD_WAVES = {
    "argocd.yaml": ("local-argocd", "-20"),
    "sealed-secrets.yaml": ("local-sealed-secrets", "-10"),
    "postgres-operator.yaml": ("local-postgres-operator", "-5"),
    "apps.yaml": ("local-apps", "0"),
    "metallb.yaml": ("local-metallb", "-19"),
    "metallb-config.yaml": ("local-metallb-config", "-18"),
    "traefik.yaml": ("local-traefik", "-17"),
    "lan-dns.yaml": ("local-lan-dns", "-16"),
    "lan-routes.yaml": ("local-lan-routes", "5"),
}


def load_yaml(path):
    return yaml.safe_load(path.read_text())


def documents(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc is not None]


def walk_values(value, path="$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_values(child, path + "." + str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_values(child, path + "." + str(index))


def crd_schema(filename):
    crd = load_yaml(OPERATOR / "vendor" / filename)
    return next(version["schema"]["openAPIV3Schema"]
                for version in crd["spec"]["versions"]
                if version["name"] == "v1" and version["served"])


def assert_schema(test, value, schema, path="$"):
    """Check the structural-schema subset used here, including fields CRDs would prune."""
    if value is None and schema.get("nullable"):
        return
    types = {
        "object": dict, "array": list, "string": str,
        "integer": int, "boolean": bool, "number": (int, float),
    }
    if "type" in schema:
        expected = types[schema["type"]]
        matches = (type(value) in expected if isinstance(expected, tuple)
                   else type(value) is expected)
        test.assertTrue(matches, f"{path}: expected {schema['type']}")
    if "enum" in schema:
        test.assertTrue(value in schema["enum"], f"{path}: outside schema enum")
    if isinstance(value, str) and "pattern" in schema:
        test.assertTrue(re.search(schema["pattern"], value) is not None,
                        f"{path}: does not match schema pattern")
    if type(value) in (int, float):
        for bound, compare in (("minimum", lambda a, b: a >= b),
                               ("maximum", lambda a, b: a <= b)):
            if bound in schema:
                test.assertTrue(compare(value, schema[bound]), f"{path}: violates {bound}")
    if isinstance(value, dict):
        test.assertTrue(set(schema.get("required", [])) <= set(value),
                        f"{path}: missing required field")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", "properties" not in schema)
        for key, child in value.items():
            child_path = path + "." + str(key)
            if key in properties:
                assert_schema(test, child, properties[key], child_path)
            elif isinstance(additional, dict):
                assert_schema(test, child, additional, child_path)
            else:
                test.assertTrue(additional or schema.get("x-kubernetes-preserve-unknown-fields"),
                                f"{child_path}: unknown field would be pruned")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_schema(test, child, schema.get("items", {}), f"{path}[{index}]")


def assert_configuration_safety(test, configuration):
    test.assertIs(configuration["enable_crd_registration"], False)
    kubernetes = configuration["kubernetes"]
    test.assertEqual(kubernetes["watched_namespace"], "databases")
    for key in ("enable_finalizers", "enable_owner_references",
                "enable_persistent_volume_claim_deletion", "enable_secrets_deletion"):
        test.assertIs(kubernetes[key], False, key)
    test.assertEqual(kubernetes["persistent_volume_claim_retention_policy"],
                     {"when_deleted": "retain", "when_scaled": "retain"})
    test.assertEqual(kubernetes["delete_annotation_date_key"], "delete-date")
    test.assertEqual(kubernetes["delete_annotation_name_key"], "delete-clustername")
    test.assertIs(kubernetes["enable_pod_antiaffinity"], True)
    test.assertEqual(kubernetes["pod_antiaffinity_topology_key"], "kubernetes.io/hostname")
    test.assertIs(kubernetes["pod_antiaffinity_preferred_during_scheduling"], False)
    for key in ("enable_teams_api", "enable_team_superuser",
                "enable_postgres_team_crd", "enable_postgres_team_crd_superusers"):
        test.assertIs(configuration["teams_api"][key], False, key)


def kustomize(path):
    result = subprocess.run(
        ["kubectl", "kustomize", str(path)], cwd=ROOT,
        capture_output=True, text=True, timeout=30, check=False,
    )
    # Do not include rendered manifests in failures: a regression could introduce Secrets.
    if result.returncode:
        raise AssertionError(f"kubectl kustomize failed for {path}: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc is not None]


class VendorTests(unittest.TestCase):
    def test_release_provenance_and_pristine_manifest_hashes(self):
        release = json.loads((OPERATOR / "release.json").read_text())
        self.assertEqual(release["repository"], REPOSITORY)
        self.assertEqual(release["tag"], TAG)
        self.assertEqual(release["commit"], COMMIT)
        self.assertEqual(release["release_url"], REPOSITORY + "/releases/tag/" + TAG)
        files = release["files"]
        self.assertEqual(len(files), 6)
        self.assertEqual({entry["manifest"] for entry in files},
                         {"vendor/" + name for name in VENDOR_FILES})
        for entry in files:
            with self.subTest(manifest=entry["manifest"]):
                name = Path(entry["manifest"]).name
                digest, size = VENDOR_FILES[name]
                source = ("https://raw.githubusercontent.com/zalando/postgres-operator/"
                          + COMMIT + "/manifests/" + name)
                self.assertEqual(entry["immutable_source"], source)
                self.assertEqual(entry["sha256"], digest)
                self.assertEqual(entry["size_bytes"], size)
                content = (OPERATOR / entry["manifest"]).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
                self.assertEqual(len(content), size)
        self.assertEqual({path.name for path in (OPERATOR / "vendor").iterdir()},
                         set(VENDOR_FILES) | {"kustomization.yaml"})

    def test_vendor_base_uses_exactly_six_local_manifests(self):
        base = load_yaml(OPERATOR / "vendor/kustomization.yaml")
        self.assertEqual(set(base), {"apiVersion", "kind", "resources"})
        self.assertEqual(base["apiVersion"], "kustomize.config.k8s.io/v1beta1")
        self.assertEqual(base["kind"], "Kustomization")
        self.assertEqual(Counter(base["resources"]), Counter(VENDOR_FILES.keys()))
        crds = [doc for name in base["resources"]
                for doc in documents(OPERATOR / "vendor" / name)
                if doc["kind"] == "CustomResourceDefinition"]
        self.assertEqual(len(crds), 2)
        self.assertEqual({doc["metadata"]["name"] for doc in crds},
                         {"postgresqls.acid.zalan.do", "operatorconfigurations.acid.zalan.do"})
        self.assertEqual({doc["spec"]["names"]["kind"] for doc in crds},
                         {"postgresql", "OperatorConfiguration"})
        self.assertTrue(all(doc["spec"]["scope"] == "Namespaced" for doc in crds))


class OperatorSourceTests(unittest.TestCase):
    def test_namespaces_stay_outside_the_namespaced_overlay(self):
        root = load_yaml(OPERATOR / "kustomization.yaml")
        self.assertEqual(set(root), {"apiVersion", "kind", "resources"})
        self.assertEqual(root["resources"], ["namespaces.yaml", "operator", "ui"])
        overlay = load_yaml(OPERATOR / "operator/kustomization.yaml")
        self.assertEqual(overlay["namespace"], "postgres-operator")
        self.assertEqual(overlay["resources"], ["../vendor"])
        for key in ("namePrefix", "nameSuffix", "helmCharts", "generators"):
            self.assertNotIn(key, overlay)
        namespaces = documents(OPERATOR / "namespaces.yaml")
        self.assertEqual(len(namespaces), 2)
        self.assertEqual({doc["metadata"]["name"] for doc in namespaces},
                         {"postgres-operator", "databases"})
        for doc in namespaces:
            self.assertEqual((doc["apiVersion"], doc["kind"]), ("v1", "Namespace"))
            self.assertNotIn("namespace", doc["metadata"])
            self.assertEqual(doc["metadata"]["annotations"], PROTECTION)

    def test_crd_protection_and_configuration_before_deployment_are_explicit(self):
        patches = load_yaml(OPERATOR / "operator/kustomization.yaml")["patches"]
        self.assertEqual([patch["path"] for patch in patches if "path" in patch],
                         ["configuration.yaml", "deployment.yaml"])
        crd_patches = [patch for patch in patches
                       if patch.get("target", {}).get("kind") == "CustomResourceDefinition"]
        self.assertEqual(len(crd_patches), 1)
        self.assertEqual(yaml.safe_load(crd_patches[0]["patch"]), [
            {"op": "add", "path": "/metadata/annotations", "value": PROTECTION},
        ])
        configuration = load_yaml(OPERATOR / "operator/configuration.yaml")
        deployment = load_yaml(OPERATOR / "operator/deployment.yaml")
        self.assertEqual(configuration["metadata"]["annotations"][SYNC_WAVE], "-5")
        self.assertEqual(deployment["metadata"]["annotations"][SYNC_WAVE], "0")

    def test_safe_configuration_overrides_match_the_vendored_schema(self):
        configuration = load_yaml(OPERATOR / "operator/configuration.yaml")
        assert_schema(self, configuration, crd_schema("operatorconfiguration.crd.yaml"))
        assert_configuration_safety(self, configuration["configuration"])
        self.assertEqual(configuration["configuration"]["logical_backup"]["logical_backup_s3_bucket"], "")
        teams_schema = crd_schema("operatorconfiguration.crd.yaml")["properties"]["configuration"][
            "properties"]["teams_api"]["properties"]
        self.assertIs(teams_schema["enable_postgres_team_crd"]["default"], True)

    def test_example_is_not_automatically_referenced(self):
        self.assertEqual({path.name for path in (OPERATOR / "examples").iterdir()},
                         {"sasakulab-postgres.yaml.example"})
        paths = list(GITOPS.rglob("kustomization.yaml"))
        paths += [APPLICATIONS / filename for filename in CHILD_WAVES]
        paths.append(APPLICATIONS / "root.yaml")
        for path in paths:
            for field, value in walk_values(load_yaml(path)):
                if isinstance(value, str):
                    with self.subTest(file=path.relative_to(GITOPS), field=field):
                        self.assertNotIn("examples", value.split("/"))
                        self.assertFalse(value.endswith(".example"))


class PostgresExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example_documents = documents(OPERATOR / "examples/sasakulab-postgres.yaml.example")
        cls.example = cls.example_documents[0]
        cls.schema = crd_schema("postgresql.crd.yaml")

    def test_inactive_example_is_valid_against_the_pinned_postgresql_crd(self):
        self.assertEqual(len(self.example_documents), 1)
        self.assertEqual(self.example["apiVersion"], "acid.zalan.do/v1")
        self.assertEqual(self.example["kind"], "postgresql")
        assert_schema(self, self.example, self.schema)

    def test_documented_credential_secret_name_normalizes_username(self):
        readme = (OPERATOR / "README.md").read_text()
        username = self.example["spec"]["databases"]["app"]
        cluster = self.example["metadata"]["name"]
        suffix = f".{cluster}.credentials.postgresql.acid.zalan.do"
        # credentialSecretNameForCluster in the pinned source replaces underscores.
        self.assertIn("kubectl -n databases get secret " + username.replace("_", "-") + suffix,
                      readme)
        self.assertNotIn("get secret " + username + suffix, readme)

    def test_schema_checker_rejects_invalid_example_fields_types_and_values(self):
        cases = [
            ("missing-required", {"teamId": None}),
            ("unknown-field", {"notAClusterField": True}),
            ("integer-version", {"postgresql": {"version": 18}}),
            ("boolean-instances", {"numberOfInstances": True}),
            ("negative-instances", {"numberOfInstances": -1}),
            ("invalid-volume", {"volume": {"size": "5GB"}}),
            ("unknown-user-flag", {"users": {"app_owner": ["not-a-role-flag"]}}),
            ("non-string-user-flag", {"users": {"app_owner": [1]}}),
            ("non-string-database-owner", {"databases": {"app": []}}),
        ]
        for label, changes in cases:
            with self.subTest(case=label):
                invalid = copy.deepcopy(self.example)
                invalid["spec"].update(changes)
                if label == "missing-required":
                    del invalid["spec"]["teamId"]
                with self.assertRaises(AssertionError):
                    assert_schema(self, invalid, self.schema)

    def test_example_is_an_opt_in_small_postgres18_test_cluster(self):
        metadata = self.example["metadata"]
        self.assertEqual(metadata["name"], "sasakulab-postgres")
        self.assertEqual(metadata["namespace"], "databases")
        self.assertEqual(metadata["annotations"][SYNC_OPTIONS], "Prune=false,Delete=false")
        spec = self.example["spec"]
        self.assertEqual(spec["teamId"], "sasakulab")
        self.assertEqual(spec["postgresql"]["version"], "18")
        self.assertEqual(spec["numberOfInstances"], 2)
        self.assertEqual(spec["volume"], {"size": "5Gi", "storageClass": "local-path"})
        self.assertEqual(spec["users"], {"app_owner": []})
        self.assertEqual(spec["databases"], {"app": "app_owner"})
        for key in ("enableMasterLoadBalancer", "enableReplicaLoadBalancer",
                    "enableConnectionPooler", "enableReplicaConnectionPooler", "enableLogicalBackup"):
            self.assertIs(spec[key], False, key)
        for path, value in walk_values(self.example):
            key = path.rsplit(".", 1)[-1].lower()
            self.assertTrue("password" not in key, f"{path}: password field must not be committed")
            self.assertTrue(key not in {"secret", "secretref", "secretkeyref", "credentials"},
                            f"{path}: example must not reference credentials")


class PostgresApplicationTests(unittest.TestCase):
    def test_child_identity_source_and_safe_sync_policy_match_peers(self):
        child = load_yaml(APPLICATIONS / "postgres-operator.yaml")
        self.assertEqual((child["apiVersion"], child["kind"]),
                         ("argoproj.io/v1alpha1", "Application"))
        self.assertEqual(child["metadata"]["name"], "local-postgres-operator")
        self.assertEqual(child["metadata"]["namespace"], "argocd")
        self.assertEqual(child["metadata"]["annotations"][SYNC_WAVE], "-5")
        self.assertNotIn("finalizers", child["metadata"])
        self.assertEqual(child["spec"]["project"], "default")
        self.assertEqual(child["spec"]["source"], {
            "repoURL": "https://github.com/m-tsuru/infra.git", "targetRevision": "main",
            "path": str(OPERATOR.relative_to(ROOT)),
        })
        self.assertEqual(child["spec"]["destination"], {
            "server": "https://kubernetes.default.svc", "namespace": "postgres-operator",
        })
        policy = child["spec"]["syncPolicy"]
        self.assertEqual(policy["automated"], {
            "enabled": True, "selfHeal": True, "prune": False, "allowEmpty": False,
        })
        self.assertEqual(set(policy["syncOptions"]), {
            "ServerSideApply=true", "FailOnSharedResource=true", "ClientSideApplyMigration=false",
        })
        for filename in ("sealed-secrets.yaml", "apps.yaml"):
            self.assertEqual(policy, load_yaml(APPLICATIONS / filename)["spec"]["syncPolicy"])
        argo_policy = load_yaml(APPLICATIONS / "argocd.yaml")["spec"]["syncPolicy"]
        self.assertEqual(policy["automated"], argo_policy["automated"])
        self.assertEqual(policy["retry"], argo_policy["retry"])
        self.assertTrue(set(policy["syncOptions"]) <= set(argo_policy["syncOptions"]))

    def test_root_has_exact_children_in_dependency_order_without_a_cycle(self):
        root = load_yaml(APPLICATIONS / "root.yaml")
        self.assertEqual((ROOT / root["spec"]["source"]["path"]).resolve(), APPLICATIONS)
        resources = load_yaml(APPLICATIONS / "kustomization.yaml")["resources"]
        self.assertEqual(Counter(resources), Counter(CHILD_WAVES.keys()))
        self.assertNotIn("root.yaml", resources)
        for filename in resources:
            child = load_yaml(APPLICATIONS / filename)
            name, wave = CHILD_WAVES[filename]
            self.assertEqual(child["metadata"]["name"], name)
            self.assertEqual(child["metadata"]["annotations"][SYNC_WAVE], wave)
            self.assertNotEqual(name, root["metadata"]["name"])
            if "chart" in child["spec"]["source"]:
                self.assertIn(filename, {"metallb.yaml", "traefik.yaml"})
                self.assertRegex(child["spec"]["source"]["targetRevision"], r"^\d+\.\d+\.\d+$")
            else:
                source = (ROOT / child["spec"]["source"]["path"]).resolve()
                self.assertNotEqual(source, APPLICATIONS)
                self.assertTrue((source / "kustomization.yaml").is_file())


@unittest.skipUnless(shutil.which("kubectl"), "kubectl is not installed; local build checks skipped")
class OperatorRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = kustomize(OPERATOR)
        cls.resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in cls.rendered}
        cls.configuration = cls.resources[("OperatorConfiguration", "postgresql-operator-default-configuration")]
        cls.deployment = cls.resources[("Deployment", "postgres-operator")]

    def test_render_is_operator_and_ui_only_with_exactly_two_crds(self):
        expected = {
            ("Namespace", "postgres-operator"), ("Namespace", "databases"),
            ("CustomResourceDefinition", "postgresqls.acid.zalan.do"),
            ("CustomResourceDefinition", "operatorconfigurations.acid.zalan.do"),
            ("ServiceAccount", "postgres-operator"), ("ClusterRole", "postgres-operator"),
            ("ClusterRole", "postgres-pod"), ("ClusterRoleBinding", "postgres-operator"),
            ("OperatorConfiguration", "postgresql-operator-default-configuration"),
            ("Deployment", "postgres-operator"), ("Service", "postgres-operator"),
            ("ServiceAccount", "postgres-operator-ui"), ("Role", "postgres-operator-ui"),
            ("RoleBinding", "postgres-operator-ui"), ("Deployment", "postgres-operator-ui"),
            ("Service", "postgres-operator-ui"),
        }
        self.assertEqual(len(expected), 16)
        self.assertEqual(set(self.resources), expected)
        self.assertEqual(len(self.rendered), len(expected))
        forbidden = {"postgresql", "postgresteam", "persistentvolumeclaim", "persistentvolume",
                     "secret", "sealedsecret", "statefulset", "cronjob"}
        self.assertTrue(all(doc["kind"].lower() not in forbidden for doc in self.rendered))
        self.assertEqual(sum(doc["kind"] == "CustomResourceDefinition" for doc in self.rendered), 2)

    def test_every_namespaced_resource_and_rbac_subject_are_transformed(self):
        cluster_kinds = {"Namespace", "CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding"}
        for doc in self.rendered:
            with self.subTest(kind=doc["kind"], name=doc["metadata"]["name"]):
                if doc["kind"] in cluster_kinds:
                    self.assertNotIn("namespace", doc["metadata"])
                else:
                    namespace = ("databases" if doc["kind"] in {"Role", "RoleBinding"}
                                 and doc["metadata"]["name"] == "postgres-operator-ui"
                                 else "postgres-operator")
                    self.assertEqual(doc["metadata"]["namespace"], namespace)
        binding = self.resources[("ClusterRoleBinding", "postgres-operator")]
        self.assertEqual(binding["subjects"], [
            {"kind": "ServiceAccount", "name": "postgres-operator", "namespace": "postgres-operator"},
        ])
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "postgres-operator",
        })
        self.assertEqual(self.deployment["spec"]["template"]["spec"]["serviceAccountName"], "postgres-operator")

    def test_rendered_waves_and_non_destructive_crd_namespace_policies(self):
        for doc in self.rendered:
            annotations = doc["metadata"]["annotations"]
            if doc["kind"] in {"Namespace", "CustomResourceDefinition"}:
                self.assertEqual(annotations[SYNC_WAVE], "-20")
                self.assertEqual(set(annotations[SYNC_OPTIONS].split(",")), {"Prune=false", "Delete=false"})
            elif doc["metadata"]["name"] == "postgres-operator-ui":
                self.assertEqual(annotations[SYNC_WAVE],
                                 "10" if doc["kind"] == "Deployment" else "5")
            elif doc["kind"] in {"ServiceAccount", "ClusterRole", "ClusterRoleBinding", "Service"}:
                self.assertEqual(annotations[SYNC_WAVE], "-10")
        config_wave = int(self.configuration["metadata"]["annotations"][SYNC_WAVE])
        deploy_wave = int(self.deployment["metadata"]["annotations"][SYNC_WAVE])
        self.assertEqual((config_wave, deploy_wave), (-5, 0))
        self.assertLess(config_wave, deploy_wave)

    def test_rendered_configuration_preserves_data_and_disables_teams_features(self):
        assert_schema(self, self.configuration, crd_schema("operatorconfiguration.crd.yaml"))
        assert_configuration_safety(self, self.configuration["configuration"])
        self.assertNotIn("finalizers", self.configuration["metadata"])
        self.assertNotIn("ownerReferences", self.configuration["metadata"])

    def test_no_load_balancers_backup_targets_credentials_or_placeholders(self):
        configuration = self.configuration["configuration"]
        for key in ("enable_master_load_balancer", "enable_replica_load_balancer",
                    "enable_master_pooler_load_balancer", "enable_replica_pooler_load_balancer"):
            self.assertIs(configuration["load_balancer"][key], False, key)
        self.assertEqual(configuration["logical_backup"]["logical_backup_s3_bucket"], "")
        unset_fields = {
            "logical_backup_s3_access_key_id", "logical_backup_s3_secret_access_key",
            "logical_backup_azure_storage_account_key", "logical_backup_google_application_credentials",
            "logical_backup_azure_storage_account_name", "logical_backup_azure_storage_container",
            "logical_backup_cronjob_environment_secret", "wal_s3_bucket", "wal_gs_bucket",
            "wal_az_storage_account", "log_s3_bucket", "gcp_credentials",
            "pod_environment_secret", "infrastructure_roles_secret_name", "infrastructure_roles_secrets",
        }
        for path, value in walk_values(configuration):
            if path.rsplit(".", 1)[-1] in unset_fields:
                self.assertTrue(not value, f"{path}: backup/credential configuration must remain unset")
            if isinstance(value, str):
                self.assertTrue(re.search(r"my-bucket-url|change[-_]?me|replace[-_]?me|your[-_](bucket|password|key)",
                                          value, re.IGNORECASE) is None,
                                f"{path}: placeholder must not be reconciled")
        self.assertIs(configuration["debug"]["debug_logging"], False)

    def test_deployment_uses_crd_configuration_and_pinned_images(self):
        pod = self.deployment["spec"]["template"]
        containers = pod["spec"]["containers"]
        self.assertEqual(len(containers), 1)
        container = containers[0]
        self.assertEqual(container["name"], "postgres-operator")
        self.assertEqual(container["image"], "ghcr.io/zalando/postgres-operator:" + TAG)
        env = container["env"]
        self.assertEqual(env, [{
            "name": "POSTGRES_OPERATOR_CONFIGURATION_OBJECT",
            "value": self.configuration["metadata"]["name"],
        }])
        self.assertNotIn("CONFIG_MAP_NAME", {item["name"] for item in env})
        revision = pod["metadata"]["annotations"]["sasakulab.com/operator-configuration-revision"]
        self.assertTrue(isinstance(revision, str) and bool(revision.strip()))
        configuration = self.configuration["configuration"]
        self.assertEqual(configuration["docker_image"], "ghcr.io/zalando/spilo-18:4.1-p2")
        self.assertEqual(configuration["logical_backup"]["logical_backup_docker_image"],
                         "ghcr.io/zalando/postgres-operator/logical-backup:" + TAG)
        self.assertEqual(configuration["connection_pooler"]["connection_pooler_image"],
                         "ghcr.io/zalando/postgres-operator/pgbouncer:" + TAG)

    def test_api_service_is_internal_and_readiness_matches_its_port(self):
        service = self.resources[("Service", "postgres-operator")]["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertTrue(set(service["selector"].items()) <=
                        set(self.deployment["spec"]["template"]["metadata"]["labels"].items()))
        self.assertEqual(service["ports"], [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}])
        container = self.deployment["spec"]["template"]["spec"]["containers"][0]
        probe = container["readinessProbe"]
        self.assertEqual(probe["httpGet"], {"path": "/readyz/", "port": 8080})
        for key in ("periodSeconds", "timeoutSeconds", "failureThreshold"):
            self.assertGreater(probe[key], 0)

    def test_rendered_root_contains_the_new_child_but_not_the_bootstrap_root(self):
        children = kustomize(APPLICATIONS)
        self.assertEqual(len(children), len(CHILD_WAVES))
        self.assertEqual({child["metadata"]["name"] for child in children},
                         {name for name, wave in CHILD_WAVES.values()})
        for child in children:
            self.assertEqual(child["kind"], "Application")
            self.assertEqual(child["metadata"]["namespace"], "argocd")
        self.assertNotIn("local-root", {child["metadata"]["name"] for child in children})


if __name__ == "__main__":
    unittest.main()
