# 0407. Apache Iceberg tables, format version 2, created by DDL

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

The lakehouse needs an open table format that Trino (query, maintenance), the Kafka Connect sink, Flink, DuckDB
and PyIceberg all write or read, with snapshots for time travel and rollback (Phases 9–14). Trino 483 supports
format versions 1–3, but on v3 "row-level updates, deletes, and OPTIMIZE are not supported"; PyIceberg and DuckDB
may default to v3 when they create tables.

## Decision

Apache Iceberg for every lake table, always `format_version = 2`, set explicitly in DDL. Bronze tables are created
by `deploy/platform/trino/base/bronze-tables.sql` (a PostSync Job runs it through Trino as `dbt`), partitioned by
`day(_ingested_at)`; no writer is allowed to create tables implicitly.

## Alternatives considered

| Option | Why not |
|---|---|
| Delta Lake | Weaker multi-engine story without Spark/Databricks; Trino maintenance procedures target Iceberg |
| Apache Hudi | Heavier write path (Spark-centric), smaller Trino feature set |
| Iceberg v3 | Trino 483 cannot OPTIMIZE/DELETE/MERGE it, which the maintenance and concurrency phases need |

## Consequences

- Positive: one table format across engines; DDL is the reviewed schema (docs/adr/0405).
- Negative / risks: v3 features (deletion vectors, variant, nanosecond timestamps) are out of scope.
- When to revisit: when Trino supports row-level operations and OPTIMIZE on v3.
