# Runbook: GoldFreshnessBurn

| | |
|---|---|
| Alert | `GoldFreshnessBurn`: `severity=page` (1h/5m or 6h/30m burn), `severity=ticket` (1d/2h or 3d/6h burn) |
| SLO | `gold-freshness`: 99% of minutes, every gold mart was refreshed less than 90 minutes ago ([`slo/gold-freshness.yaml`](../../slo/gold-freshness.yaml)) |
| SLI | `gold:refresh_stale:minute` ([`gold-freshness-sli.prometheusrule.yaml`](../../deploy/platform/slo/base/gold-freshness-sli.prometheusrule.yaml)) |

## What it means

A gold mart (`lake.gold.*`, read by Metabase) has had no data commit for 90 minutes or more, its freshness probe
fails, or the freshness exporter is gone. dbt rebuilds gold every hour (Airflow DAG `dbt_build`), so a healthy mart
is at most about one hour old. The age comes from `data_refresh_age_seconds{table="gold.<mart>"}`: the latest
snapshot in `lake_ro.gold."<mart>$snapshots"`, not counting `replace` snapshots (the daily optimize and manifest
rewrite change no rows, so they are not a refresh).

Only clusters with profile `batch` count, from 2 hours after namespace `airflow` was created.

## Triage

1. Which mart and how old: in Grafana Explore, `data_refresh_age_seconds{table=~"gold.*"}` and
   `freshness_probe_success{table=~"gold.*"}`. All marts old → dbt is not running. One mart old → that model fails.
   Probe failing → [freshness-probe-down](freshness-probe-down.md) (same exporter, same Trino path).
2. Is `dbt_build` running? Airflow UI (`kubectl -n airflow port-forward svc/airflow-api-server 8080`): DAG paused,
   runs queued (scheduler down), or failing. Scheduler health:
   `kubectl -n airflow get pods -l component=scheduler` and its logs.
3. A failing task → [dbt-failure](dbt-failure.md). `cdc_epoch_guard` failing means no CDC epoch has completed: see
   [cdc-lag](cdc-lag.md); gold would otherwise be rebuilt from an empty silver.
4. Trino reachable from Airflow? A task log with TLS or 401 errors: Secret `airflow/trino-dbt` (copied from
   `lakehouse/trino-dbt` by the copy Job) and `airflow/airflow-trino-ca`.

## Mitigation

- DAG paused: unpause it; the next hourly run refreshes every mart (or trigger `dbt_build` by hand).
- Scheduler down or OOMKilled: it runs the dbt tasks itself (LocalExecutor); check its memory limit in
  `deploy/platform/airflow/local/values.yaml`.
- A model failing: fix it (dbt-failure), then trigger `dbt_build`. Gold stays readable at its last good version
  while the alert fires: dashboards show stale numbers, not wrong ones.
