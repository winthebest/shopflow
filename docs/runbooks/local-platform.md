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
| More profiles | `make up CLUSTER=... PROFILES=core,obs-lite` (`obs` and `obs-lite` are exclusive; `data` needs one of them; `rt`, `batch`, `bi` need `data`) |
| What is running | `make status CLUSTER=...` |
| Argo CD UI | `make platform-argocd-ui CLUSTER=...` then open `https://localhost:18080` (`sf-main`; lanes 18081–18084) |
| Argo CD password | `make platform-argocd-password` copies it to the clipboard (user `admin`) |
| Delete cluster + registry | `make down CLUSTER=...` |
| Validate `deploy/` like CI | `make platform-validate` |

`make up` is idempotent: on an existing cluster it re-applies Argo CD and the root apps and waits for every
Application to be `Synced` + `Healthy` (`WAIT_TIMEOUT`, default 900s).

Profile changes on an existing cluster:

- Switching `obs` ↔ `obs-lite`: `make up` deletes the other root app, then the apps only it deploys (e.g. loki,
  tempo, otel-collector); apps both profiles share (kube-prometheus-stack, slo, grafana-dashboards) keep running
  and are adopted by the new root app. Other profiles that are no longer listed are only reported, not removed.
  The app lists come from the profiles in your local checkout: pull before switching, so they match the revision
  the cluster tracks.
- With `data`: right after the root apps, `make up` runs `scripts/cdc-epoch.sh new` (sf-data), so every `make up`
  starts a new CDC epoch (ADR 0406).

## Root apps and session parameters

`make up` and `make cloud-up` both create root apps through `scripts/platform-root-apps.sh` (ADR 0206):

```bash
scripts/platform-root-apps.sh --print --overlay local --revision main --profiles core     # what would be applied
scripts/platform-root-apps.sh --check --overlay aws --revision main --profiles core \
  --param aws.region=ap-southeast-1 ...                                                   # validate a cloud call
```

- New parameter: declare it in `deploy/argocd/profiles/_common/platform-params.yaml` (and in
  `shopflow.io/required-aws` if cloud-up must always pass it), then copy it into a Helm value with a top-level
  replacement in the aws profile. `make platform-validate` fails on undeclared keys.

## Image pull-through caches

Every node of every local cluster pulls through four shared caches (ADR 0207): `k3d-shopflow-cache-docker`
(docker.io), `-quay`, `-ghcr`, `-k8s` (registry.k8s.io) on `127.0.0.1:5060–5063`. `make up` creates them on first
use; their data lives in Docker volumes of the same names and survives `make down`, so each image is downloaded
from the internet once per machine.

- Only clusters created after the caches exist use them (`registries.yaml` is read when a node starts).
- If a cache is down, containerd falls back to the upstream registry; pulls are slower but nothing breaks.
- Disk use: `make status`. Reset: `make platform-cache-down` (deletes caches + data). Opt out: `PULL_CACHE=0 make up`.
- Caches are anonymous: never configure registry credentials on them (private images would be served to anyone).

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
   a schema check fails, if an image has no digest, or if the shop migration image (`sha-<commit>` tag of
   `services.orders`) was built before the newest commit under `services/orders/migrations`. In platform-ci that
   last check fails on main and on PRs that change `deploy/charts/shop`, and only warns on other PRs: after a
   migration merges, main stays red until a PR bumps the shop images to a tag built from it (or later).

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

## AWS: seeding sf-platform secrets

On AWS the shop-db role passwords come from SSM through External Secrets (same Secret names and keys as the local
SOPS files). Seed them once, before the first session that needs them:

```bash
scripts/platform-secrets.sh --aws-json | scripts/aws-seed-params.sh
```

- The producer prints one random password per role enabled in `deploy/charts/shop-db/values-aws.yaml` and refuses
  to write to a terminal; values never appear in arguments or files.
- **Keep these passwords stable.** The roles come back with the database restored every session, so the SSM value
  must keep matching the role. Seeding keeps existing parameters, so re-running the pipe changes nothing. Rotate only
  on purpose: `aws-seed-params.sh --rotate` together with an `ALTER ROLE ... PASSWORD` in the same session.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `address already in use` on 655x/84xx/50xx | A stale cluster or process owns the port: `docker ps`, `k3d cluster list`, stop the owner. Do not pick another port. |
| `revision ... not found on origin` | Push the branch first. |
| `failed to get ready: error waiting for log line \`successfully registered node\`` (agent) | Docker/k3d start-up race; k3d rolls back and `make up` retries the create once by itself. A second failure stops: check `docker info` and free memory, then `make down` and `make up`. |
| `revision '<short sha>' not found on origin` | A commit pin must be the full 40-character SHA (`git rev-parse <sha>`). |
| App `ComparisonError ... ksops` | `sops-age` Secret missing or wrong key: rerun `make up`; check `age-keygen -y <key>` matches the recipient in `.sops.yaml`. |
| App stuck `OutOfSync` on a CR | CRD from an earlier wave not ready: check that wave's app; CRs carry `SkipDryRunOnMissingResource=true`. |
| Pods `ContainerCreating` for minutes | Image pulls are slow on a cold cache; `kubectl describe pod` shows `Pulling`. |
| Fix pushed but the app keeps failing on the old commit (`operationState` still `Running`, retries counting up) | Retries are pinned to the commit that failed. Terminate the operation (UI: *Sync status → Terminate*, or API `DELETE /api/v1/applications/<app>/operation` through `make platform-argocd-ui`); the next auto-sync takes the new commit. |
| PreSync hook Job `FailedCreate ... serviceaccount not found` | A PreSync hook runs before every resource of its own app; hook pods must only use objects that already exist (the chart's Jobs use the `default` ServiceAccount). |
