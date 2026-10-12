# 0416. Metabase for BI on gold, with its dashboard provisioned from git through the API

- Status: Accepted
- Date: 2026-10-12
- Lane: sf-data

## Context

Phase 5 needs one BI tool that people can explore with: daily revenue, payment success rate and top products, read
from the gold marts only. Constraints:

- Access: Trino catalog `lake_ro` as user `metabase` (docs/adr/0410), over HTTPS with a password.
- Memory: the `bi` profile has about 1 GB on the laptop.
- Lifetime: every cluster is rebuilt from git, and an AWS session cluster lives for hours.
- Safety: no path may write into the lake.

## Decision

- **Tool and deployment.** Metabase OSS (`metabase/metabase`, pinned by digest) in profile `bi`, namespace `metabase`:
  one pod from plain kustomize manifests.
- **Trino connection.** The Starburst driver ships with the OSS build (`modules/drivers/starburst` in Metabase's
  repository) and supports Trino. The connection verifies Trino's TLS certificate against the Trino CA.
- **Application database.** CNPG shop-db, database and role `metabase`.
- **Admin, connection and dashboard from git.** A PostSync Job calls Metabase's API:
  - it runs setup with an admin from a SOPS Secret, so the setup wizard is never left open;
  - it then creates or updates, by name, the Trino connection, one question per SQL file in the repo, and the
    dashboard.
  The OSS build has no serialization (export/import is a paid feature), so the dashboard is code, rebuilt on every
  cluster instead of restored from a backup.
- **No write path.** Metabase's write features (actions, uploads) are off. The login could not write anyway:
  `lake_ro` is a READ_ONLY catalog, and Trino's rules give `metabase` SELECT on schema `gold` only.
- **Top products.** These need a gold mart, because `metabase` cannot read silver: `mart_product_sales` holds units
  and revenue per UTC day and product, for paid orders.

## Alternatives considered

| Option | Why not |
|---|---|
| Apache Superset | Its chart still depends on Bitnami subcharts (Postgres, Redis), and async queries need Celery workers and Redis: more pods and memory for one dashboard |
| Grafana (already deployed) with a Trino datasource plugin | Community plugin; panels are built by the dashboard author, not explored by the reader; mixes business BI into the operations Grafana |
| Metabase serialization (export/import) | Paid feature only |
| Metabase application database in H2 on a volume | Lost with the cluster and never backed up |

## Consequences

- Positive:
  - one pod;
  - the dashboard's SQL is reviewed in pull requests;
  - a new cluster, local or AWS, has the dashboard without manual steps and needs nothing restored.
- Negative / risks:
  - The provisioning script depends on Metabase's REST API, which can change between releases. The image is pinned,
    the Job fails loudly when a call fails, and an upgrade must run it once.
  - Questions and the dashboard the Job owns are overwritten on every sync, so edits made in the UI to them are lost.
    Questions and dashboards created in the UI are left alone.
  - The JVM's memory is not measured yet; a short `bi` slot measures it before `bi` joins a longer slot.
- When to revisit: people need many dashboards edited in the UI (serialization or another tool), or Metabase needs
  more memory than the `bi` budget.
