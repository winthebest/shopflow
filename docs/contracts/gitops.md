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

- Experiment branches: `<lane>/exp-<topic>` may change any file (including other lanes' values) to drive an
  experiment on the lane's own cluster (e.g. `PAYMENT_LATENCY_MS=600`, game days). They are never opened as PRs or
  merged; findings land through normal PRs by the file owner.

## 1b. Overlays and session parameters (ADR 0206)

- Local: `deploy/argocd/apps/<c>/` + `deploy/argocd/profiles/<p>/` (as above). AWS: sibling trees
  `deploy/argocd/apps-aws/<c>/` (`resources: [../../apps/<c>]` + patches swapping `local` value files/paths for `aws`)
  and `deploy/argocd/profiles-aws/<p>/` (lists `../../apps-aws/<c>`, or `../../apps/<c>` for apps that are identical
  or AWS-only; same inline revision block). Owners: same as the local app/profile. Kustomize forbids an overlay
  that includes its own parent directory, hence siblings.
- `scripts/platform-root-apps.sh --overlay local|aws --revision R --profiles P --param k=v...` (sf-platform) is the
  only way to create root apps; `make up` and `cloud-up` both call it. Unknown param keys are rejected; on `aws`,
  missing required keys are rejected.
- Session params live in `deploy/argocd/profiles/_common/platform-params.yaml` (local-config ConfigMap, keys with
  dots) and reach components **only as Helm values** (`spec.sources.[chart=...].helm.valuesObject.<path>`, via
  top-level replacements in the aws profile). A component that needs a param or param-driven logic is a Helm chart
  (in-repo if needed, e.g. `deploy/charts/shop-db`).
- On AWS, External Secrets and the AWS LB Controller run in `profiles-aws/core` at wave -1, Envoy Gateway at wave 0
  with `loadBalancerClass: service.k8s.aws/nlb` (never a Classic ELB). OpenCost needs Prometheus, so it belongs to
  `profiles-aws/obs` and `profiles-aws/obs-lite`.
- On AWS, each component's `ExternalSecret`s live in that component's aws overlay (owner = component owner, like the
  local SOPS files); sf-cloud's external-secrets app holds the controller and the per-namespace
  ClusterSecretStores only. ExternalSecrets carry `SkipDryRunOnMissingResource=true`.

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
- Profiles `rt` (Flink), `batch` (Airflow) and `bi` (Metabase) need `data` in the same `PROFILES` (they read Kafka,
  Trino or shop-db through data's components); `platform-root-apps.sh` rejects them without it.
- Profile `ops` needs `data` in the same `PROFILES`: its only workload is the fulfillment-worker, which reads Kafka and
  is scaled by KEDA on consumer lag (revisit if Phase 8 adds a component that does not need Kafka).
- Profile `data` needs `obs` or `obs-lite` in the same `PROFILES` (its ServiceMonitors and rules need their CRDs);
  `make up` rejects `data` without one of them.
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
| `ops` | sf-sre | keda (sf-sre) and fulfillment-worker (sf-app: a second Argo CD app of the shop chart that renders only the worker, sharing its image pins), always on from Phase 7; needs `data` |
| `chaos` | sf-sre | chaos-mesh, **game days only**: added with the session, its root app deleted right after (CI fails if any other profile lists chaos-mesh); experiments may target only namespaces annotated `chaos-mesh.org/inject=enabled` (`enableFilterNamespace`) |

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
  - Otherwise (env from a same-namespace Secret, e.g. Polaris, Trino) an in-cluster copy Job owned by the consumer
    component reads the origin (Role in the origin namespace: `get` with `resourceNames`) and writes the copy in its
    own namespace (Role: `create`, plus `get`/`update` with `resourceNames`), in a sync wave before the consumer and
    re-run on every sync. No second copy in git, no drift on rotation.
- On AWS every origin Secret comes from SSM through External Secrets (same names/keys as the local SOPS files);
  derived copies keep coming from the in-cluster copy Jobs. One writer per Secret: ESO never writes a Secret a
  Job or a generator also writes.
- Lanes never decrypt with the user's age private key (`sops -d`, KSOPS builds on a laptop). Encrypting new SOPS
  files needs only the public recipient in `.sops.yaml`. The only decryption outside the cluster is the bootstrap in
  `make up` (ADR 0204); anything else needs the user's approval through the orchestrator.
- Generated values (passwords) are random at creation time. Values only the user knows (for example a chat
  webhook URL) start as a clearly marked placeholder; the user replaces them with `sops <file>` later.
- Known secrets (name → keys):

| Namespace | Secret | Keys | Owner |
|---|---|---|---|
| `argocd` | `argocd-admin` | `password` | sf-platform |
| `observability` | `grafana-admin` | `admin-user`, `admin-password` | sf-sre |
| `observability` | `alertmanager-webhook` | `url` (mounted as a file) | sf-sre (placeholder; user sets real URL) |
| `shop` | `shop-db-app` | created by CNPG (`uri`, `password`, …) | sf-platform (CNPG) |
