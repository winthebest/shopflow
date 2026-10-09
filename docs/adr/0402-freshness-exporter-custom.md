# 0402. Custom freshness exporter for lakehouse tables

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

The `cdc-lag` SLO (Phase 4) and `gold-freshness` SLO (Phase 5) need "how old is the newest data in table X", read
through Trino with password authentication over HTTPS, for a configured list of tables. Red-team finding 10: when
the probe itself breaks, the SLO must not stay green, and alerts need an "exporter absent" path.

## Decision

`services/freshness-exporter`: a small Python service (prometheus-client + trino client) that every 60s runs
`SELECT max("_source_ts_ms")` per configured table on catalog `lake_ro` as user `exporter`, and exports:

- `data_freshness_seconds{table}` — now minus the newest source commit time;
- `freshness_probe_success{table}` — 0 on query error, timeout or empty table;
- `freshness_last_success_timestamp{table}`.

A failed probe removes `data_freshness_seconds{table}` instead of keeping the last value, so a broken probe can
never look fresh; the SLO counts such a minute as bad. `process_start_time_seconds` (default registry) lets the
alert rules inhibit freshness alerts for 15 minutes after a restart. Table and catalog names are validated as
plain identifiers before they are quoted into SQL. Logs follow the shop log contract (JSON lines).

## Alternatives considered

| Option | Why not |
|---|---|
| Prometheus SQL exporters (sql_exporter, query-exporter) | Generic config, no "failed probe removes the series" semantics, Trino HTTPS + password support uneven |
| Trino JMX metrics | Expose engine health, not data age per table |
| Measure in the sink (commit timestamps) | Says when data was written, not how old the source changes are; misses a stalled Debezium |

## Consequences

- Positive: ~60 lines of logic with unit tests for every failure path; one place to add the Phase 5 snapshot-based
  gold freshness.
- Negative / risks: one more image to build and patch; each probe is a Trino query (cheap: metadata + one column).
- When to revisit: if an OSS exporter gains per-table SQL probes with absent-on-failure semantics.
