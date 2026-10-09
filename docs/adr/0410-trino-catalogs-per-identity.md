# 0410. Trino as the query engine, with the safety boundary at the catalog

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

dbt (Phase 5), Metabase, the freshness exporter and later the AI planner/executor (Phases 9–14) all query the
lake; dbt also reconciles against Postgres. Trino's file-based access control cannot restrict table procedures
(`expire_snapshots`, `remove_orphan_files`, `rollback_to_snapshot`, `optimize`): any identity that reaches a
writable catalog can run them (docs/contracts/environment.md).

## Decision

Trino 483 (chart 1.42.2, coordinator-only locally, 1.5GB heap) with three catalogs:

| Catalog | Mode | Identities | Catalog credentials |
|---|---|---|---|
| `lake` | read/write + procedures | `dbt` (later `executor`) | Polaris `trino_lake`, S3 read/write |
| `lake_ro` | `iceberg.security=READ_ONLY` | `exporter`, `metabase` (gold only) | Polaris `trino_lake_ro`, S3 read-only |
| `pg` | Postgres `shop`, read only | `dbt` | DB role `trino_pg` (SELECT) |

File-based access control (`rules.json` in `deploy/platform/trino/base/values.yaml`) is the single list of
identities; Phases 11/14 extend it. Nobody may change `expire_snapshots_min_retention` or
`remove_orphan_files_min_retention` by session property. The retention floors on `lake` (and `lake_ro`) are a
hand-set constant `7d`; `scripts/data-validate.sh` fails if they change.

## Alternatives considered

| Option | Why not |
|---|---|
| Only per-table rules in one writable catalog | Table procedures bypass them |
| DuckDB as the shared engine | In-process, no multi-user auth; kept as a laptop tool (`make duckdb`) |
| Spark SQL / Thrift server | Much heavier for laptop-sized data |
| Athena only | AWS only; local and cloud would differ |

## Consequences

- Positive: three independent layers for read-only users (Trino catalog mode, Polaris principal, S3 identity).
- Negative / risks: duplicated catalog properties for `lake` and `lake_ro`; Trino holds four credential Secrets.
- When to revisit: Trino adds procedure-level access control, or the Phase 9 refusal matrix finds a gap.

## Open question (Phase 6, AWS)

On AWS `lake` and `lake_ro` use Glue (docs/adr/0506) and S3 through Pod Identity: Trino has one ServiceAccount, so
both catalogs share one IAM role with write access, and there is no Polaris principal. Read-only then rests on the
engine layer alone (`iceberg.security=READ_ONLY`, the boundary in docs/contracts/environment.md); the S3 and catalog
layers of the table above are lost, a defence-in-depth gap rather than a broken boundary. To decide in Phase 6, with
sf-cloud: accept it and record it, or restore a storage-level layer (Lake Formation permissions, or a separate
identity for read-only queries).
