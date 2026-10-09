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

## 4. Profiles

- Profile = a directory `deploy/argocd/profiles/<profile>/kustomization.yaml`:

  ```yaml
  apiVersion: kustomize.config.k8s.io/v1beta1
  kind: Kustomization
  resources:
    - ../../apps/<component>      # app directories
  components:
    - ../_common                  # git-revision ConfigMap + replacement into every shopflow source
  ```

  `deploy/argocd/profiles/_common/` (sf-platform) is a Kustomize `Component` holding the `git-revision`
  ConfigMap and the replacement into `spec.sources.[repoURL=https://github.com/winthebest/shopflow.git].targetRevision`.
  `make up PROFILES=core,obs` creates one root Application per profile and patches the ConfigMap with the
  revision (`main` on `sf-main`, the lane branch on a lane cluster). Labels are metadata only.
- Profile files and their owners:

| Profile | Owner | Contents |
|---|---|---|
| `core` | sf-platform | Argo CD self-management (optional), Gateway API CRDs, Envoy Gateway, cert-manager, CNPG, shop, network policies |
| `obs-lite` | sf-sre | kube-prometheus-stack, slo, grafana-dashboards |
| `obs` | sf-sre | everything in `obs-lite` + loki, tempo, otel-collector |
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
- Generated values (passwords) are random at creation time. Values only the user knows (for example a chat
  webhook URL) start as a clearly marked placeholder; the user replaces them with `sops <file>` later.
- Known secrets (name → keys):

| Namespace | Secret | Keys | Owner |
|---|---|---|---|
| `argocd` | `argocd-admin` | `password` | sf-platform |
| `observability` | `grafana-admin` | `admin-user`, `admin-password` | sf-sre |
| `observability` | `alertmanager-webhook` | `url` (mounted as a file) | sf-sre (placeholder; user sets real URL) |
| `shop` | `shop-db-app` | created by CNPG (`uri`, `password`, …) | sf-platform (CNPG) |
