# Runbook: reconciliation failed (Airflow DAG `reconciliation`)

| | |
|---|---|
| Signal | Task `reconcile` of DAG `reconciliation` failed (daily 04:45 UTC) |
| Test | [`data/dbt/tests/generic/reconciles_with_postgres.sql`](../../data/dbt/tests/generic/reconciles_with_postgres.sql) on every silver table |
| Decision | docs/adr/0413 (silver), 0412 (reconciliation tests, var `reconcile`) |

## What it means

For at least one shop table, silver and Postgres disagree on a row that is not explained by CDC lag: a key on one
side only, or a different status/amount. Rows changed after silver's last data commit minus 15 minutes are left out
(`reconcile_lag_minutes`), so ordinary staleness between hourly runs does not count. Silver is wrong, Postgres is
the source of truth.

## Triage

1. Which table and rows: the task log lists the failing test (`reconciles_with_postgres_<table>_...`) and its row
   count. To see the rows, run the compiled SQL from the log in Trino as `dbt` (it returns the key and
   `missing in silver`, `missing in postgres` or `values differ`).
2. `missing in silver` / `values differ` for recent rows: CDC lagging more than 15 minutes? Check
   `CdcLagBurn` / [cdc-lag](cdc-lag.md). Was the last `dbt_build` run successful ([dbt-failure](dbt-failure.md))?
3. `missing in postgres`: a row deleted in Postgres within the lag window (deletes carry no `updated_at`; the shop
   itself never deletes), or a delete that never reached bronze (Debezium connector failed at that time:
   [connector-failed](connector-failed.md)).
4. Old rows that differ: a change that never reached bronze, e.g. CDC was down and the slot was lost. Compare
   `bronze.<table>` for that key: missing `u`/`d` rows in the current `_cdc_epoch`?

## Mitigation

- Lag or a failed dbt run: rerun `dbt_build`, then clear `reconcile`.
- Changes lost in CDC: re-snapshot into a new epoch (`scripts/cdc-epoch.sh new`, restart the connectors,
  `scripts/cdc-epoch.sh wait`), then `dbt_build` and `reconcile`. Silver switches to the new epoch, and a quiet-point
  check (`--vars '{reconcile: true, reconcile_lag_minutes: 0}'` right after a rebuild) must then pass.
