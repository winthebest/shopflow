# Contract: local environment, clusters, ports, identities

## Docker and clusters

- Docker Desktop VM: 16GB. At most **2 k3d clusters at the same time**, and their profiles must fit ~11GB together
  (measured: `core` ≈ 3.3GB; `obs` adds ≈ 2.5GB; `data` adds ≈ 5–6GB). `sf-main` (orchestrator, pinned to a `main`
  SHA) runs only during gates. Lane clusters hold **slots** assigned by the orchestrator; ask before `make up`,
  run `make down` when your cluster work is done.
- Deterministic names and ports. On "address in use", stop and report; never pick another port.

| Cluster | Owner | API port | HTTPS (load balancer) | Local registry (API − 1500) | Argo CD UI port-forward (API + 11530) |
|---|---|---|---|---|---|
| `sf-main` | orchestrator | 6550 | 9443 | 5050 | 18080 |
| `sf-platform` | sf-platform | 6551 | 8443 | 5051 | 18081 |
| `sf-sre` | sf-sre | 6552 | 8444 | 5052 | 18082 |
| `sf-data` | sf-data | 6553 | 8445 | 5053 | 18083 |
| `sf-app` | sf-app | 6554 | 8446 | 5054 | 18084 |

All bind `127.0.0.1`. The registry is created with the cluster and removed by `k3d cluster delete`.

- Host ports for docker compose stacks (bind to `127.0.0.1` only). Lanes declare new host ports here before using them:

| Port | Stack | Owner |
|---|---|---|
| 25432 | `docker-compose.yml` Postgres (15432 belongs to incident-lab) | sf-app |
| 8000 | `docker-compose.yml` gateway | sf-app |
| 5060–5063 | shared pull-through image caches `shopflow-cache-{docker,quay,ghcr,k8s}` (one per upstream; data in named volumes that survive `make down`; `make platform-cache-down` removes them) | sf-platform |
- Cluster creation scripts take the cluster name and ports as parameters (default `sf-main`).

## Namespaces

| Namespace | Contents |
|---|---|
| `shop` | gateway, orders, payments, `shop-db` (CNPG), migration Job |
| `argocd` | Argo CD |
| `envoy-gateway-system`, `cert-manager`, `cnpg-system` | platform controllers |
| `observability` | OTel Collector, Prometheus, Loki, Tempo, Grafana (Phase 3) |
| `kafka`, `lakehouse`, `airflow`, `bi` | data platform (Phases 4–5) |
| `external-secrets`, `opencost` (+ AWS LB Controller in `kube-system`) | AWS only (Phase 6, sf-cloud) |

## Network flows (NetworkPolicy allow-list, Phase 6 security baseline; local too)

Default-deny per namespace; these flows must be allowed. Owners add rows when they introduce a new flow.

| From | To | Port | Why | Requested by |
|---|---|---|---|---|
| `shop` pods | `observability` otel-gateway | 4317, 4318 | OTLP traces/metrics | sf-sre |
| `observability` Prometheus | `shop` CNPG exporter | 9187 | Postgres metrics (PodMonitor) | sf-sre |
| `observability` OTel agent/gateway | kube-apiserver | 443/6443 | `k8sattributes` processor | sf-sre |
| `shop` gateway | `shop` orders | 8001 | service call | sf-app |
| `shop` orders | `shop` payments, `shop-db` | 8002, 5432 | service call, DB | sf-app |
| `envoy-gateway-system` | `shop` gateway | 8000 | ingress | sf-platform |
| `kafka` cdc-connect | `shop` shop-db | 5432 | Debezium (replication) | sf-data |
| `kafka` cdc-connect | `lakehouse` polaris, seaweedfs | 8181, 8333 | Iceberg sink: catalog, objects | sf-data |
| `kafka` cdc-connect, `lakehouse` polaris-setup Job | kube-apiserver | 443/6443 | read Secret via config provider; write principal Secrets | sf-data |
| `lakehouse` trino | `lakehouse` polaris, seaweedfs | 8181, 8333 | catalogs `lake`, `lake_ro` | sf-data |
| `lakehouse` trino | `shop` shop-db | 5432 | catalog `pg` | sf-data |
| `lakehouse` polaris | `shop` shop-db; `lakehouse` seaweedfs | 5432; 8333 | metadata DB `catalog`; object store | sf-data |
| `lakehouse` polaris-setup Job | `lakehouse` polaris | 8181 | bootstrap principals | sf-data |
| `lakehouse` trino-bronze-tables Job | `lakehouse` trino | 8443 | create bronze tables | sf-data |
| `observability` Prometheus | `lakehouse` freshness-exporter; `kafka` cdc-connect | 8080; 9404 | scrape freshness + Connect/Debezium JMX metrics | sf-data |
| `lakehouse` freshness-exporter | `lakehouse` trino | 8443 | freshness probe queries (`lake_ro`, user `exporter`) | sf-data |
| `external-secrets` controller | AWS STS/SSM; Pod Identity agent | 443; 169.254.170.23:80 | SSM → Secrets (AWS only) | sf-cloud |
| `kube-system` aws-load-balancer-controller | AWS APIs; kube-apiserver → controller webhook | 443; 9443 | NLB for the Gateway (AWS only) | sf-cloud |
| `opencost` | `observability` kps-prometheus; AWS pricing | 9090; 443 | cost allocation (AWS only) | sf-cloud |
| `observability` Prometheus | `opencost` | metrics port | scrape OpenCost (AWS only) | sf-cloud |
| `lakehouse` polaris-db-copy, trino-pg-copy Jobs | kube-apiserver | 443/6443 | copy `shop/shop-db-{polaris,trino-pg}` into `lakehouse` (gitops.md §5) | sf-data |

## Trino catalogs and identities (Phase 4 onwards)

Trino file-based access control cannot restrict table procedures (`expire_snapshots`, `remove_orphan_files`,
`rollback_to_snapshot`, `optimize`): any identity that can reach a writable catalog can run them. Isolation is
therefore per catalog.

| Catalog | Mode | Identities |
|---|---|---|
| `lake` | read/write, procedures | `dbt`, later `executor` |
| `lake_ro` | `iceberg.security=read_only` | `metabase` (gold only), `exporter`, later `agent_planner` |
| `pg` | read-only Postgres `shop` | `dbt` (reconciliation) |

Retention floors on `lake` (`iceberg.expire-snapshots.min-retention`, `iceberg.remove-orphan-files.min-retention`)
are a hand-set constant of `7d`, never derived from policy files and never `0s`.

## Secrets

- Local: SOPS + age. Age private key lives only at `~/.config/sops/age/keys.txt` on the user's machine (with an
  offline backup). Lanes never print, copy or commit it.
- Standing user approval (2026-10-09): a lane may run `make up` / `make down` on its own k3d cluster. `make up` uses
  the key only to decrypt the Argo CD admin password and to load it into the cluster for KSOPS (ADR 0204). Any other
  use of the private key (`sops -d`, laptop KSOPS builds) still needs the user's approval through the orchestrator.
- No plaintext secrets in git; gitleaks runs in pre-commit and CI.
- AWS (Phase 6): SSM Parameter Store via External Secrets Operator. Only `sf-cloud` touches AWS, and only after the
  user approves each session through the orchestrator.

## Make targets

- Each lane defines targets only in its own `mk/<lane>.mk`.
- Public targets named in the plan/README keep their short names and belong to one lane:
  `dev`, `dev-down`, `dev-reset`, `dev-logs` (sf-app); `up`, `down`, `status` (sf-platform);
  `cloud-up`, `cloud-down`, `cloud-pause`, `cloud-resume`, `cloud-extend` (sf-cloud); `duckdb` (sf-data).
- Every other target is prefixed with the lane: `app-*`, `platform-*`, `sre-*`, `data-*`, `cloud-*`.
- Each target has a `## description` comment so `make help` lists it.
