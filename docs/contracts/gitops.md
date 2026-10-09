# Contract: GitOps conventions (Argo CD, Helm, secrets, profiles)

Owner: orchestrator. Mechanics (root apps, KSOPS plugin, `.sops.yaml`) are implemented by sf-platform; every
component owner follows these conventions for its own component.

## 1. One Argo CD Application per component

- Directory: `deploy/argocd/apps/<component>/` with `application.yaml` + `kustomization.yaml`
  (`resources: [application.yaml]`), owned by the component owner. Directories (not loose files) let profiles
  reference apps under Kustomize's default load restrictor; `LoadRestrictionsNone` is **not** used.
- `spec.sources` is always a **list** (even with one source) and contains at least one source with
  `repoURL: https://github.com/winthebest/shopflow.git`; the profile replacement sets its `targetRevision`.
  An app without such a source fails the build on purpose, so no app silently stays on `main`.
- Git source: `repoURL: https://github.com/winthebest/shopflow.git`. `targetRevision` is `main` on `sf-main`;
  a lane cluster may point at the lane branch (the root app passes the revision as a parameter).
- Labels: `app.kubernetes.io/part-of: shopflow`, `shopflow.io/profile: <profile>`.
- Sync policy: automated + selfHeal + prune; `CreateNamespace=true`; `ServerSideApply=true` for charts with
  large CRDs (for example kube-prometheus-stack).

## 2. Helm-based components: multi-source Application

No `--enable-helm` in Kustomize. Use an Argo CD multi-source Application:

```yaml
sources:
  - repoURL: <chart repo>            # chart version pinned
    chart: <chart>
    targetRevision: <exact version>
    helm:
      valueFiles:
        - $values/deploy/platform/<component>/base/values.yaml
        - $values/deploy/platform/<component>/local/values.yaml   # or aws/values.yaml in the aws overlay
  - repoURL: https://github.com/winthebest/shopflow.git
    targetRevision: <revision>
    ref: values
  - repoURL: https://github.com/winthebest/shopflow.git   # optional: extra manifests (rules, dashboards, CRs)
    targetRevision: <revision>
    path: deploy/platform/<component>/local
```

Plain-manifest components use a single Kustomize source at `deploy/platform/<component>/<overlay>`.
Third-party images are pinned by digest (Helm values or Kustomize `images:`).

## 3. Sync waves (across the app-of-apps)

| Wave | What |
|---|---|
| -3 | Kyverno (Phase 8) |
| -2 | CRD-only apps (Gateway API CRDs, etc.) |
| -1 | Operators/controllers (cert-manager, Envoy Gateway, CNPG, kube-prometheus-stack, Strimzi, …) |
| 0 | Platform config and stateful backends (GatewayClass/Gateway, ClusterIssuer, Loki, Tempo, OTel Collector, Kafka, SeaweedFS, catalog, Trino) |
| 1 | Workloads and resources that need CRDs from earlier waves (shop, PrometheusRule/SLO, dashboards, connectors) |

Config that belongs to one controller may instead live inside that controller's app with an in-app
`argocd.argoproj.io/sync-wave` and `SkipDryRunOnMissingResource=true` when it needs CRDs from the same sync
(for example cert-manager's ClusterIssuers, Envoy Gateway's GatewayClass/Gateway).

## 4. Profiles

- Profile = a directory `deploy/argocd/profiles/<profile>/kustomization.yaml` with this exact shape:

  ```yaml
  apiVersion: kustomize.config.k8s.io/v1beta1
  kind: Kustomization
  resources:
    - ../../apps/<component>      # app directories
  components:
    - ../_common                  # only the git-revision ConfigMap
  replacements:                   # MUST stay at the profile top level (not inside the Component)
    - source: {kind: ConfigMap, name: git-revision, fieldPath: data.revision}
      targets:
        - select: {kind: Application}
          fieldPaths:
            - spec.sources.[repoURL=https://github.com/winthebest/shopflow.git].targetRevision
  ```

  Why top level: Kustomize applies a Component before the profile's top-level `patches`. The root app sets the
  revision with a top-level patch (`spec.source.kustomize.patches`), so a replacement inside the Component would
  copy the unpatched value (`main`). Verified with `kubectl kustomize` (2026-10-09): replacement in the Component →
  child stays on `main`; replacement at top level → child follows the patched revision.
- `deploy/argocd/profiles/_common/` (sf-platform) is a Kustomize `Component` with `resources: [git-revision.yaml]`
  only. `make up PROFILES=core,obs` creates one root Application per profile and patches the ConfigMap with the
  revision (`main` on `sf-main`, the lane branch on a lane cluster). Labels are metadata only.
- Fail-closed check: `scripts/platform-validate.sh` (sf-platform, run in platform-ci) builds every profile with a
  test revision patched the same way as the root app and fails if any shopflow source is not on that revision.
- `obs` and `obs-lite` are mutually exclusive (both own the `otel-gateway` release in `observability`);
  `make up` rejects `PROFILES` containing both.
- Profile files and their owners:

| Profile | Owner | Contents |
|---|---|---|
| `core` | sf-platform | Argo CD self-management (optional), Gateway API CRDs, Envoy Gateway, cert-manager, CNPG, shop, network policies |
| `obs-lite` | sf-sre | kube-prometheus-stack, slo, grafana-dashboards, otel-collector-lite (only the `otel-gateway` Deployment: spanmetrics for SLIs, traces dropped after metrics, no log agent) |
| `obs` | sf-sre | kube-prometheus-stack, slo, grafana-dashboards, loki, tempo, otel-collector (full: gateway → Tempo + log agent DaemonSet) |
| `data` | sf-data | strimzi, kafka, kafka-connect, seaweedfs, iceberg-catalog, trino |
| `rt`, `batch`, `bi` | sf-data | flink, airflow, metabase |
| `ops` | sf-sre | chaos-mesh (game days only), keda (Kyverno added by Phase 8) |

## 5. Secrets (SOPS + age + KSOPS)

- `.sops.yaml` (sf-platform) has a creation rule matching `deploy/**/secrets/*.enc.yaml`, encrypting with the
  user's age **public** recipient. Encrypting only needs the public key; decrypting needs the private key, which
  only the user and the in-cluster KSOPS secret hold.
- Each component owner keeps its encrypted secrets next to its overlay:
  `deploy/platform/<component>/<overlay>/secrets/<name>.enc.yaml`, plus a KSOPS generator in that overlay.
  `deploy/secrets/` is only for cluster-wide secrets owned by sf-platform (for example Argo CD admin).
- KSOPS `files:` paths are relative to the directory of the overlay's `kustomization.yaml`, not to the generator
  file (`./secrets/<name>.enc.yaml`); `platform-validate.sh` checks this.
- Cross-namespace credentials (one origin per password):
  - Read in place when the consumer can: Strimzi reads `shop/shop-db-debezium` through
    `KubernetesSecretConfigProvider`; a Role in `shop` grants `get` on that one Secret (`resourceNames`) to the
    consumer's ServiceAccount. The Role/RoleBinding belong to the consumer component.
  - Otherwise (env from a same-namespace Secret, e.g. Polaris, Trino) the consumer's owner derives its own SOPS file
    from the origin in a pipe: `sops -d <origin> | yq '<new name/namespace/keys>' | sops -e --filename-override <dest>
    /dev/stdin > <dest>`. Never displayed, never written in plaintext; re-derive when the origin rotates.
- Generated values (passwords) are random at creation time. Values only the user knows (for example a chat
  webhook URL) start as a clearly marked placeholder; the user replaces them with `sops <file>` later.
- Known secrets (name → keys):

| Namespace | Secret | Keys | Owner |
|---|---|---|---|
| `argocd` | `argocd-admin` | `password` | sf-platform |
| `observability` | `grafana-admin` | `admin-user`, `admin-password` | sf-sre |
| `observability` | `alertmanager-webhook` | `url` (mounted as a file) | sf-sre (placeholder; user sets real URL) |
| `shop` | `shop-db-app` | created by CNPG (`uri`, `password`, …) | sf-platform (CNPG) |
