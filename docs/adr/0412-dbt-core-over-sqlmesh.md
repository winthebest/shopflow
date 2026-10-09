# 0412. dbt Core with dbt-trino for bronze → silver → gold

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Phase 5 turns append-only bronze (docs/adr/0406) into current-state silver and business gold marts, with tests,
an exact reconciliation against Postgres and a CI check. Trino is the only engine that reaches both the lake and
Postgres (docs/adr/0410), so the transformation tool must run SQL through Trino.

## Decision

dbt Core 1.12 with dbt-trino 1.10 (pinned in `data/dbt/uv.lock`), project `data/dbt`, user `dbt` on catalog `lake`
(writes) and `pg` (reads):

- Staging models are ephemeral, silver and gold are Iceberg tables (docs/adr/0413).
- Schemas are used as named (`silver`, `gold`), not dbt's `<schema>_<custom>`.
- Tests: unique, not_null, relationships, accepted_values, source freshness on `bronze.heartbeat`, and the generic
  test `reconciles_with_postgres` (two-way anti-join on the key plus status/money columns; tag `reconciliation`,
  enabled only with `--vars '{reconcile: true}'`, so the hourly run, whose Airflow tasks are rendered from the default
  manifest, never contains it).
- CI (`make data-dbt-check`): `dbt parse` and sqlfluff (Trino dialect, Jinja templater with dbt builtins). Neither
  needs a database.
- `profiles.yml` takes everything from the environment (Secret `trino-dbt`, CA from `trino-tls`), TLS validation
  required.

## Alternatives considered

| Option | Why not |
|---|---|
| SQLMesh | Stronger incremental/plan model, but the data here is small enough to rebuild in full and dbt is what the job market and the Airflow tooling (Cosmos) expect |
| Hand-written SQL in Airflow tasks | No lineage, tests or docs; every model would need its own runner |
| Spark / Flink batch jobs | Heavy for laptop-sized data; SQL through Trino is enough |

## Consequences

- Positive: lineage, tests and docs in one tool; the reconciliation is an ordinary dbt test the daily DAG selects
  by tag.
- Negative / risks: dbt has no catalog-level guard against table procedures; the boundary stays at the Trino catalog
  (docs/adr/0410).
- When to revisit: full rebuilds exceed the 5-minute budget, or dbt-trino lags behind Trino releases.
