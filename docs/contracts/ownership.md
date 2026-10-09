# Contract: who owns which paths

One owner per path. To change a path you don't own, message the orchestrator; the owner or the orchestrator
makes the change. Shared root files are owned by the orchestrator.

| Path | Owner lane | Phases |
|---|---|---|
| `README.md`, `Makefile`, `.gitignore`, `.github/CODEOWNERS`, `.github/pull_request_template.md`, `docs/contracts/`, `docs/adr/README.md`, `docs/adr/0000-*`, `docs/adr/0001-*` | orchestrator | 0 |
| `services/` (gateway, orders, payments, later fulfillment-worker), `docker-compose.yml`, `loadtest/`, `scripts/seed.py`, `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`, `.gitleaks.toml`, `.github/workflows/ci.yml`, `mk/app.mk`, `docs/perf-baseline.md` | sf-app | 1, 4–5 (migrations, contracts), 7 (app side) |
| `data/contracts/`, `scripts/check_contracts.py` | sf-app | 5 |
| `scripts/k3d-*.sh`, `deploy/argocd/`, `deploy/charts/shop/`, `deploy/platform/{envoy-gateway,cert-manager,cnpg,network-policies,storageclass}/`, `.sops.yaml`, `deploy/secrets/`, `.github/workflows/platform-ci.yml`, `mk/platform.mk` | sf-platform | 2, 6 (security baseline) |
| `deploy/platform/{otel-collector,kube-prometheus-stack,loki,tempo,slo,grafana-dashboards,chaos-mesh,keda}/`, `slo/`, `chaos/`, `docs/slo/`, `docs/runbooks/checkout-*`, `docs/postmortems/`, `mk/sre.mk` | sf-sre | 3, 7 |
| `images/kafka-connect/`, `deploy/platform/{strimzi,kafka,kafka-connect,seaweedfs,iceberg-catalog,trino,flink-operator,flink,airflow,metabase}/`, `data/{flink,dbt,airflow}/`, `services/freshness-exporter/`, `scripts/cdc-epoch.sh`, `.github/workflows/{connect-image,data-ci}.yml`, `mk/data.mk` | sf-data | 4, 5 |
| `infra/`, `scripts/{cloud-*,aws-*,export-evidence}.sh`, `deploy/platform/{external-secrets,aws-lb-controller,opencost}/`, `.github/workflows/{infra-ci,cloud-reaper}.yml`, `docs/cost.md`, `docs/runbooks/{cloud-session,restore}.md`, `mk/cloud.mk` | sf-cloud | 6, 7 (restore drill with sf-sre) |
| `deploy/platform/<component>/aws/` | owner of `<component>` | 6 |
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
