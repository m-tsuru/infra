# Local cluster GitOps

Flux reconciles its own vendored controllers, its Git source and root sync, and
all additional Kubernetes resources from this tree. k3s still owns its packaged
CoreDNS, metrics-server and local-storage; this tree does not replace them or
install Traefik/ServiceLB. Host DNS and k3s configuration belong to the surrounding
Ansible setup.

## Reconciliation layout

- `clusters/local/flux-system/gotk-components.yaml`: byte-for-byte official Flux
  installation manifest.
- `clusters/local/flux-system/gotk-sync.yaml`: public, read-only HTTPS Git source
  `https://github.com/m-tsuru/infra`, branch `main`, and the root reconciliation
  path `./ansible/svc/k3s-setup/gitops/clusters/local`.
- `clusters/local/kustomization.yaml`: includes Flux itself and the
  `sealed-secrets` and `apps` Flux Kustomizations.
- `infrastructure/sealed-secrets`: upstream controller, CRD and RBAC, managed by
  the `sealed-secrets` Kustomization with `wait: true`.
- `apps`: initially `resources: []`. Register each new manifest in its
  `kustomization.yaml`. Its Flux Kustomization waits for resources to be healthy
  and depends on `sealed-secrets` being Ready.

The root deliberately does **not** set `wait: true`: it includes its own Flux
Kustomization and must not recursively wait for itself. Child readiness is
checked separately by Flux and by bootstrap. All three Flux Kustomizations use
`prune: true`; removing resources from Git can delete live resources. Keep
persistent data and namespace deletion consequences in mind.

## Ansible role interface

The surrounding playbook must invoke `gitops_bootstrap` **only on the selected
bootstrap server**, after `/usr/local/bin/k3s` and
`/etc/rancher/k3s/k3s.yaml` are available. The role uses only `ansible.builtin`
modules, becomes root itself, and invokes `/usr/local/bin/k3s kubectl` with that
kubeconfig. It does not select a server from inventory, install CLI tools, or
require a Flux CLI, Helm, kubeseal, or Kubernetes Ansible collection on the node.

The only public variable is:

| Variable | Default | Purpose |
| --- | --- | --- |
| `gitops_bootstrap_force` | `false` | Recovery: bootstrap even when the root exists and allow server-side apply field conflicts to be overwritten. |

Repository URL, branch, reconciliation paths, object names and controller/CRD
wait targets come from committed manifests, not parallel Ansible settings.
Private-repository authentication/bootstrap is **not implemented**. There is no
Git credential Secret or write token; image-automation controllers included by
the official Flux manifest have no configured Git write access.

Commit and push this complete tree to `main` **before** running bootstrap. Flux
fetches GitHub, not the operator's uncommitted checkout. Missing files or a
private/unreachable repository will cause readiness waits to fail. Bootstrap
requires Kubernetes API access, GitHub HTTPS access and controller image pulls
(GHCR for Flux and Docker Hub for Sealed Secrets).

### Bootstrap and recovery behavior

1. Probe the root Kustomization CRD and object without hiding API/auth errors.
2. If the root exists, skip all bootstrap mutations and waits, even if it is
   currently unhealthy or suspended. Normal Ansible reruns must not fight Flux.
3. Otherwise stage only the two committed Flux manifests in a temporary root-only
   directory. Create the manifest's namespace if absent, then use server-side
   diff/apply with field manager `flux-client-side-apply`, which Flux can adopt.
4. Wait for the vendored Flux CRDs to be Established (180-second timeout) and
   all vendored controller Deployments to be Available (300-second timeout).
5. Only then diff/apply `gotk-sync.yaml`. Wait for the root and both child Flux
   Kustomizations to be Ready (600-second timeout for each wait).
6. Remove staged files even when a command fails.

Diff exit codes distinguish unchanged (0), changed (1) and errors (>1). Only
namespace creation and actual apply differences report changes; staging, probes
and waits do not. Apply/diff/probe API requests have a 30-second request timeout;
readiness API requests are also bounded by the corresponding wait timeout.
Check mode performs only the read-only existence probes and reports whether
bootstrap would be needed; it does not test full cluster readiness.

For deliberate recovery, pass `-e gitops_bootstrap_force=true` to the surrounding
playbook. Use a checkout matching the desired remote branch: forcing can take
field ownership and replace bootstrap settings, and Flux will subsequently
reconcile the remote Git version again. Force does not delete/recreate the
cluster, reset Sealed Secrets keys, or repair every possible CRD migration.

## Sealing application secrets

Install the matching `kubeseal` **v0.40.0** client on the operator workstation and
use a kubeconfig that can reach the cluster. The upstream controller name is
`sealed-secrets-controller`, in `kube-system`. Explicitly pass both flags; do not
assume a Helm release name or a `sealed-secrets` namespace.

For example, from the repository root, with the real token already stored in a
protected local file (replace `/secure/path/token`; do not put it in this repo):

```sh
kubectl create secret generic app-credentials \
  --namespace=default \
  --from-file=token=/secure/path/token \
  --dry-run=client --output=json |
  kubeseal --controller-namespace=kube-system \
    --controller-name=sealed-secrets-controller --format=yaml \
    > ansible/svc/k3s-setup/gitops/apps/app-credentials-sealedsecret.yaml
```

The plaintext Secret is piped directly to kubeseal, not saved or applied. Add the
resulting **SealedSecret** filename to `apps/kustomization.yaml`, review it for
plaintext/metadata leaks, then commit and push. For a non-default namespace,
commit its Namespace manifest as well. Default sealing binds ciphertext to the
Secret's name and namespace: changing either requires resealing. Never commit
plain Kubernetes Secrets, tokens, private keys or the controller's sealing keys.

Sealing keys are generated by the controller and persist as Secrets in
`kube-system`. Back them up securely **outside Git** before relying on this
cluster: losing them during a rebuild makes existing SealedSecrets undecryptable.
Encrypted manifests can be public, but their metadata is public too. Applications
also need to handle Secret delivery/startup; depending on controller readiness
alone does not guarantee every application has consumed its Secret.

## Pinned releases and upgrades

These official release assets were downloaded and their SHA256 digests verified
before writing the vendored files:

| Component | Version / asset | SHA256 |
| --- | --- | --- |
| Flux | [v2.9.6/install.yaml](https://github.com/fluxcd/flux2/releases/download/v2.9.6/install.yaml) | `9c1fda7e401429531ed1478f67ba06b3edff6513b75da7ee47bde9f1c4d4251c` |
| Sealed Secrets | [v0.40.0/controller.yaml](https://github.com/bitnami/sealed-secrets/releases/download/v0.40.0/controller.yaml) | `ac8aabccdd9110430d117501705818115b2ab0993700a9c118d4d42cf4015a17` |

The target k3s version is `v1.37.1+k3s1`; cluster compatibility/runtime behavior
must still be validated on that target by the operator. No cluster commands are
needed to build the manifests locally with Kustomize.

For an upgrade, review upstream release notes and Kubernetes/CRD compatibility,
download the exact official release asset, verify its SHA256 against a trusted
release checksum, replace the corresponding vendored file **without modifying
it**, and update this table. Update the operator's kubeseal client alongside the
Sealed Secrets controller. Review controller arguments, RBAC and CRD changes,
build `clusters/local`, `infrastructure/sealed-secrets` and `apps` with Kustomize,
then commit and push. Flux applies normal upgrades; **do not force Ansible** as
the routine upgrade mechanism. Back up sealing keys and persistent data first;
CRD upgrades may not be safely reversible just by reverting Git.
