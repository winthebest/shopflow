# Contract: local environment, clusters, ports, identities

## Docker and clusters

- Docker Desktop VM: 16GB. At most **2 k3d clusters at the same time**: `sf-main` (orchestrator, tracks `main`)
  plus one lane cluster holding the **slot**. Ask the orchestrator for the slot; delete your cluster when done.
- Deterministic names and ports. On "address in use", stop and report; never pick another port.

| Cluster | Owner | API port | HTTPS (load balancer) |
|---|---|---|---|
| `sf-main` | orchestrator | 6550 | 9443 |
| `sf-platform` | sf-platform | 6551 | 8443 |
| `sf-sre` | sf-sre | 6552 | 8444 |
| `sf-data` | sf-data | 6553 | 8445 |
| `sf-app` | sf-app | 6554 | 8446 |

- `docker-compose.yml` (sf-app dev loop): Postgres on host port **25432** (15432 belongs to incident-lab).
- Cluster creation scripts take the cluster name and ports as parameters (default `sf-main`).

## Namespaces

| Namespace | Contents |
|---|---|
| `shop` | gateway, orders, payments, `shop-db` (CNPG), migration Job |
| `argocd` | Argo CD |
| `envoy-gateway-system`, `cert-manager`, `cnpg-system` | platform controllers |
| `observability` | OTel Collector, Prometheus, Loki, Tempo, Grafana (Phase 3) |
| `kafka`, `lakehouse`, `airflow`, `bi` | data platform (Phases 4–5) |

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
- No plaintext secrets in git; gitleaks runs in pre-commit and CI.
- AWS (Phase 6): SSM Parameter Store via External Secrets Operator. Only `sf-cloud` touches AWS, and only after the
  user approves each session through the orchestrator.
