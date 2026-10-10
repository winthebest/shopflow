# Runbook: Iceberg maintenance failed (Airflow DAG `iceberg_maintenance`)

| | |
|---|---|
| Signal | A failed task `maintain_<schema>` of DAG `iceberg_maintenance` (daily 03:30 UTC) |
| Code | [`data/dbt/macros/iceberg_maintenance.sql`](../../data/dbt/macros/iceberg_maintenance.sql), [`data/airflow/dags/iceberg_maintenance.py`](../../data/airflow/dags/iceberg_maintenance.py) |
| Floors | 7-day `expire_snapshots` / `remove_orphan_files` minimum on catalog `lake` (docs/adr/0410) |

## What it means

For one schema (`bronze`, `silver` or `gold`), a table maintenance step failed. For every table, the macro runs
`optimize` (compacts small files: bronze gets one file per sink commit), then `optimize_manifests`, then
`expire_snapshots` and `remove_orphan_files` with a 7-day retention. Nothing is lost when it fails: queries keep
working, but files and snapshots accumulate (slower bronze scans, more storage). One failed day is harmless;
several in a row are not.

## Triage

Read the task log (Airflow UI). The macro logs every statement before running it, so the last `alter table ...
execute ...` line names the table and step.

- `retention ... is shorter than the minimum retention`: someone lowered `retention` below the catalog floor; it must
  stay at 7d or more (`iceberg.*.min-retention` in `deploy/platform/trino/base/values.yaml`; `make data-validate`
  fails if the floor changes).
- Commit conflict on `optimize` (`CommitFailedException`, `Cannot commit ... found conflicting files`): a concurrent
  writer. Bronze: the Iceberg sink commits every 60 seconds; optimize only rewrites, the sink only appends, and
  Iceberg retries, so a repeated conflict points to something else rewriting the table. Silver/gold: a `dbt_build`
  run overlapping 03:30 (it rebuilds tables at :00, normally within minutes).
- Catalog or storage errors: Polaris / SeaweedFS (as in [cdc-lag](cdc-lag.md), step 4).
- Out of memory in Trino on `optimize` of a large bronze partition: Trino's memory is sized for laptop data
  (`deploy/platform/trino/local/values.yaml`).

## Mitigation

Fix the cause and clear the failed task: every step is idempotent, and a table that was already maintained is not
harmed by a second pass. The macro stops at the first failing statement, so the tables after it in that schema
(alphabetical order) were not maintained either. While one table keeps failing, maintain the others by hand with the
four `ALTER TABLE ... EXECUTE` statements in Trino as `dbt` (the log shows them), or rerun
`dbt run-operation iceberg_maintenance --args '{schema: <schema>}'` from the scheduler pod once it is fixed.
