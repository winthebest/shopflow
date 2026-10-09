# Contract: who owns which paths

One owner per path. To change a path you don't own, message the orchestrator; the owner or the orchestrator
makes the change. Shared root files are owned by the orchestrator.

| Path | Owner lane | Phases |
|---|---|---|
| `README.md`, `Makefile`, `.gitignore`, `.github/CODEOWNERS`, `.github/pull_request_template.md`, `docs/contracts/`, `docs/adr/README.md`, `docs/adr/0000-*`, `docs/adr/0001-*` | orchestrator | 0 |
| `services/` (gateway, orders, payments, later fulfillment-worker), `docker-compose.yml`, `loadtest/`, `scripts/seed.py`, `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`, `.gitleaks.toml`, `.github/workflows/ci.yml`, `mk/app.mk`, `docs/perf-baseline.md` | sf-app | 1, 4–5 (migrations, contracts), 7 (app side) |
| `data/contracts/`, `scripts/check_contracts.py` | sf-app | 5 |
| `scripts/k3d-*.sh`, `scripts/platform-*.sh`, `deploy/argocd/root-app*.yaml` and app-of-apps mechanics, `deploy/argocd/apps/{envoy-gateway,cert-manager,cnpg,shop-db,shop,network-policies}/`, `deploy/charts/shop/`, `deploy/platform/{envoy-gateway,cert-manager,cnpg,shop-db,network-policies,storageclass}/`, `.sops.yaml`, `deploy/secrets/`, `.github/workflows/platform-ci.yml`, `mk/platform.mk` | sf-platform | 2, 6 (security baseline) |
| `deploy/platform/{otel-collector,kube-prometheus-stack,loki,tempo,slo,grafana-dashboards,chaos-mesh,keda}/` and `slo/` (SLO toolchain, windows, every SLO except the `cdc*` files owned by sf-data; sf-sre reviews those), `chaos/`, `docs/slo/`, `docs/runbooks/checkout-*`, `docs/postmortems/`, `.github/workflows/sre-ci.yml`, `scripts/sre-*.sh`, `mk/sre.mk` | sf-sre | 3, 7 |
| `images/kafka-connect/`, `deploy/platform/{strimzi,kafka,kafka-connect,seaweedfs,iceberg-catalog,trino,flink-operator,flink,airflow,metabase}/`, `data/{flink,dbt,airflow}/`, `services/freshness-exporter/`, `deploy/platform/freshness-exporter/`, `scripts/cdc-epoch.sh`, `scripts/data-*.sh`, `.github/workflows/{data-images,data-ci}.yml`, `slo/cdc*.yaml`, `slo/tests/cdc*.test.yaml`, `deploy/platform/slo/base/cdc-*.prometheusrule.yaml`, `mk/data.mk` | sf-data | 4, 5 |
| `infra/`, `scripts/{cloud-*,aws-*,export-evidence}.sh`, `deploy/platform/{external-secrets,aws-lb-controller,opencost}/`, `.github/workflows/{infra-ci,cloud-reaper}.yml`, `docs/cost.md`, `docs/runbooks/{cloud-session,restore}.md`, `mk/cloud.mk` | sf-cloud | 6, 7 (restore drill with sf-sre) |
| `deploy/platform/<component>/aws/` | owner of `<component>` | 6 |
| `deploy/argocd/apps/<component>/` (one Argo Application per component) | owner of `<component>` | 2–7 |
| `deploy/argocd/profiles/_common/` (incl. `platform-params.yaml`), `scripts/platform-root-apps.sh`, `deploy/charts/{shop-db,edge}/`, `deploy/argocd/bootstrap/` (incl. `values-aws.yaml`) | sf-platform | 2, 6 |
| `deploy/argocd/apps-aws/<component>/` | owner of `<component>` | 6 |
| `deploy/argocd/profiles-aws/<profile>/` | same owner as the local profile (`gitops.md` §4) | 6 |
| `deploy/argocd/profiles/<profile>/` | see `gitops.md` §4 (core: sf-platform; obs, obs-lite, ops: sf-sre; data, rt, batch, bi: sf-data) | 2–7 |
| `deploy/platform/<component>/<overlay>/secrets/*.enc.yaml` | owner of `<component>` (`deploy/secrets/` is only for cluster-wide secrets, sf-platform) | 2–7 |
| `docs/runbooks/<topic>.md` | lane that owns the alert/topic | 3–7 |

ADR numbering ranges (no collisions between parallel lanes):

| Range | Lane |
|---|---|
| 0001–0099 | orchestrator |
| 0100–0199 | sf-app |
| 0200–0299 | sf-platform |
| 0300–0399 | sf-sre |
| 0400–0499 | sf-data |
| 0500–0599 | sf-cloud |
