# Runbook: local platform (k3d + Argo CD)

Owner: sf-platform. Conventions: `docs/contracts/environment.md` (clusters, ports) and `docs/contracts/gitops.md`
(apps, profiles, waves, secrets).

## Prerequisites

- Docker Desktop running (VM 16GB), plus `k3d kubectl helm sops yq jq htpasswd git`.
- The age private key at `~/.config/sops/age/keys.txt` (override with `SOPS_AGE_KEY_FILE`). Never print or copy it.
- The Git revision you deploy must be pushed: Argo CD reads GitHub, not your checkout.

## Everyday commands

| Goal | Command |
|---|---|
| Integration cluster on `main` | `make up` (cluster `sf-main`, profile `core`) |
| Lane cluster on your branch | `make up CLUSTER=sf-platform GIT_REVISION=$(git branch --show-current)` |
| More profiles | `make up CLUSTER=... PROFILES=core,obs-lite` (`obs` and `obs-lite` are exclusive) |
| What is running | `make status CLUSTER=...` |
| Argo CD UI | `make platform-argocd-ui CLUSTER=...` then open `https://localhost:18080` (`sf-main`; lanes 18081–18084) |
| Argo CD password | `make platform-argocd-password` copies it to the clipboard (user `admin`) |
| Delete cluster + registry | `make down CLUSTER=...` |
| Validate `deploy/` like CI | `make platform-validate` |

`make up` is idempotent: on an existing cluster it re-applies Argo CD and the root apps and waits for every
Application to be `Synced` + `Healthy` (`WAIT_TIMEOUT`, default 900s).

## How a change reaches the cluster

1. Commit and push to the branch the cluster tracks.
2. Argo CD polls every ~60s, renders the profile, and syncs the changed Applications in wave order.
3. Never `kubectl apply` by hand: `selfHeal` reverts manual changes.

## Local registry

Each cluster has a registry on `127.0.0.1:<API_PORT − 1500>` (`sf-main` 5050, `sf-platform` 5051, …):

```bash
docker tag my-image localhost:5051/my-image:dev && docker push localhost:5051/my-image:dev
# in manifests on that cluster: image: sf-platform-registry:5000/my-image:dev
```

Only for the inner dev loop; anything committed uses GHCR images pinned by digest.

## Adding a component (any lane)

1. `deploy/argocd/apps/<component>/application.yaml` + `kustomization.yaml` (`resources: [application.yaml]`).
2. Use `spec.sources` (a list) with at least one source from `https://github.com/winthebest/shopflow.git`; Helm
   charts follow the multi-source pattern in `gitops.md` §2. Set the wave annotation from `gitops.md` §3.
3. Add `- ../../apps/<component>` to your profile's `resources`. Keep the profile's inline `replacements` block.
4. `make platform-validate`: fails if a profile or app does not render, if any app ignores the root revision, if
   a schema check fails, or if an image has no digest.

## Adding a secret

```bash
# create in plain text only inside sops' editor; the file is encrypted on save
sops deploy/platform/<component>/local/secrets/<name>.enc.yaml
```

Content is a normal `Secret` manifest (namespace set). Then reference it from a KSOPS generator in the overlay:

```yaml
# deploy/platform/<component>/local/secrets/ksops-generator.yaml
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: <component>-secrets
  annotations:
    config.kubernetes.io/function: |
      exec:
        path: ksops
files:
  # relative to the overlay directory (where kustomize runs), not to this generator file
  - ./secrets/<name>.enc.yaml
```

and `generators: [secrets/ksops-generator.yaml]` in the overlay's `kustomization.yaml`. For a random password
without seeing it: generate it in a pipe and encrypt from stdin with
`sops -e --filename-override <path> /dev/stdin > <path>`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `address already in use` on 655x/84xx/50xx | A stale cluster or process owns the port: `docker ps`, `k3d cluster list`, stop the owner. Do not pick another port. |
| `revision ... not found on origin` | Push the branch first. |
| App `ComparisonError ... ksops` | `sops-age` Secret missing or wrong key: rerun `make up`; check `age-keygen -y <key>` matches the recipient in `.sops.yaml`. |
| App stuck `OutOfSync` on a CR | CRD from an earlier wave not ready: check that wave's app; CRs carry `SkipDryRunOnMissingResource=true`. |
| Pods `ContainerCreating` for minutes | Image pulls are slow on a cold cache; `kubectl describe pod` shows `Pulling`. |
