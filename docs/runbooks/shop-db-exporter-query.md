# Runbook: a shop-db exporter query fails

| | |
|---|---|
| Alert | `ShopDbExporterQueryFailing` (ticket) |
| Rule | [`deploy/platform/slo/base/shop-db-exporter-queries.prometheusrule.yaml`](../../deploy/platform/slo/base/shop-db-exporter-queries.prometheusrule.yaml) (tests: `slo/tests/shop-db-exporter-queries.test.yaml`) |
| Queries | [`deploy/charts/shop-db/templates/monitoring-queries.yaml`](../../deploy/charts/shop-db/templates/monitoring-queries.yaml) (ConfigMap `shop-db-monitoring`) |
| Also checked by | `scripts/sre-gate-check.sh`: FAIL on any failing query, and on a missing `dag_run` series with profile `batch` up |

## What it means

shop-db's exporter runs custom SQL queries and turns their rows into metrics. For example, `airflow_dag_run` reads
Airflow's `dag_run` and feeds `AirflowDagRunFailed`. A query that fails exports nothing: its metrics vanish, and every
alert on them stays silent instead of firing. Nothing else shows it: the collector's error gauge
(`cnpg_collector_last_collection_error`) stays 0, and the exporter's target stays up.

The trace is `cnpg_errors_total`. Despite the name, it is not a counter that grows: it holds one series per query that
failed on the last collection, with value 1, and the series disappears once the query works again. Its label
`errorUserQueries` reads `<query> on db <database>: <error>`. The alert fires after 5 minutes, so the failures at
bring-up, before a database exists, do not count.

Which alerts are blind depends on the query:

| Query | Metrics | Blind alert |
|---|---|---|
| `airflow_dag_run` | `cnpg_airflow_dag_run_last_failed`, `cnpg_airflow_dag_run_last_end_time_seconds` | `AirflowDagRunFailed`: a failed reconciliation or Iceberg maintenance goes unnoticed, and a failed dbt run is noticed only once gold goes stale ([gold-stale](gold-stale.md)) |

## Triage

1. Read the error in Grafana Explore (`kubectl -n observability port-forward svc/kps-grafana 3000:80`): query
   `cnpg_errors_total`. The label `errorUserQueries` gives the query, the database and the Postgres error.
2. By error:
   - `permission denied for table …` (SQLSTATE 42501): the exporter runs queries as `cnpg_metrics_exporter`, a member
     of `pg_monitor`, not as a superuser. A table owned by an application role needs an explicit grant to
     `pg_monitor` on the columns the query reads. Check with:
     `SELECT has_column_privilege('pg_monitor', 'dag_run', 'state', 'SELECT');` in that database. For `dag_run`,
     the grant belongs in Airflow's migration Job, right after `airflow db migrate`.
   - `database "…" does not exist` (SQLSTATE 3D000), longer than bring-up: shop-db creates its extra databases with
     CNPG `Database` objects (values `databases` in the chart), whatever the profiles. The object is missing or
     failing: `kubectl -n shop get databases.postgresql.cnpg.io`. The query's predicate cannot skip a missing database:
     the connection fails first.
   - `relation … does not exist`, `column … does not exist`: the application's migration changed the table. Update
     the query in `monitoring-queries.yaml` together with that change.
3. The alert resolves on the next collection after the query works again. Then check that the series it feeds are
   back, e.g. `cnpg_airflow_dag_run_last_failed`.
