# Runbook: dbt run failed (Airflow DAG `dbt_build`)

| | |
|---|---|
| Signal | A failed task in DAG `dbt_build` (Airflow UI); gold goes stale, then [`GoldFreshnessBurn`](gold-stale.md) |
| Code | [`data/airflow/dags/dbt_build.py`](../../data/airflow/dags/dbt_build.py), [`data/dbt`](../../data/dbt) |
| Decisions | docs/adr/0412 (dbt), 0413 (silver from the latest completed epoch), 0414 (Airflow, Cosmos) |

## What it means

The hourly run builds silver (current state of the latest completed CDC epoch) and gold, one Airflow task per model
run and per model's tests. A failed run task leaves that table at its previous version; a failed test task stops
the models downstream of it, so a broken silver table never reaches gold. Tasks retry once after 5 minutes.

## Triage

Open the failed task's log in the Airflow UI (`kubectl -n airflow port-forward svc/airflow-api-server 8080`; admin
login in Secret `airflow/airflow-admin`). By task:

- `cdc_epoch_guard`: no row of `pg.meta.cdc_epochs` has `snapshot_completed_at`. CDC was reset and its snapshot has
  not finished: `CLUSTER=<cluster> scripts/cdc-epoch.sh wait`, see [cdc-lag](cdc-lag.md).
- `models.<model>.run`, Trino errors:
  - `TABLE_NOT_FOUND` on `lake.bronze.*`: the bronze DDL Job (`trino-bronze-tables`) did not run;
  - `Access Denied`: `rules.json` in `deploy/platform/trino/base/values.yaml` (user `dbt`: `lake` all, `pg` read);
  - catalog or storage errors: Polaris or SeaweedFS (as in [cdc-lag](cdc-lag.md), step 4).
- `models.<model>.test`: a data test failed. Find the failing rows with
  `dbt test --select <test> --store-failures` or the compiled SQL in the log. Typical causes: a new status value in
  Postgres (accepted_values; update `models/silver/silver.yml` with the Alembic CHECK constraint), duplicates in
  silver (unique; a bronze row without `_lsn`?), relationships broken by a delete in Postgres.
- `models.relationships_*`: runs after both parents; a failure is a real orphan, not an ordering problem.
- `source_freshness`: `bronze.heartbeat` older than 30 minutes, which means CDC is behind. It does not block the
  models, see [cdc-lag](cdc-lag.md).

## Mitigation

Fix the cause, then clear the failed task (or trigger the DAG): the run is idempotent, every table is rebuilt in
full (`on_table_exists: replace`). Do not skip a failing test task to unblock gold: gold would be built from the
data the test rejected.
