# SLO: gold freshness

Owner: sf-data (sf-sre reviews). Spec: [`slo/gold-freshness.yaml`](../../slo/gold-freshness.yaml) → generated rules
[`deploy/platform/slo/base/gold-freshness-slo.prometheusrule.yaml`](../../deploy/platform/slo/base/gold-freshness-slo.prometheusrule.yaml)
(Sloth, [ADR 0300](../adr/0300-slo-tooling-sloth.md)). SLI rule:
[`gold-freshness-sli.prometheusrule.yaml`](../../deploy/platform/slo/base/gold-freshness-sli.prometheusrule.yaml).
Tests: [`slo/tests/gold-freshness.test.yaml`](../../slo/tests/gold-freshness.test.yaml). Runbook:
[gold-stale](../runbooks/gold-stale.md).

## Why gold

The gold marts (`lake.gold.*`: `fct_orders`, `dim_customers`, `mart_daily_revenue`, `mart_payment_success_rate`,
`mart_customer_ltv`) are what Metabase shows. dbt rebuilds them every hour (Airflow DAG `dbt_build`, profile `batch`;
ADR 0412–0414). Stale gold means the business looks at old numbers without knowing it.

## SLI

| SLO | Good minute | Objective | Error budget (28 days) |
|---|---|---|---|
| `gold-freshness` | every gold mart's latest data commit is less than 90 minutes old | **99%** | ≈ 6h43m of stale gold |

Per-minute recording rule `gold:refresh_stale:minute` (1 = bad, 0 = good, no sample = not counted):

- **Age** = `data_refresh_age_seconds{table="gold.<mart>"}` from the freshness exporter (`REFRESH_TABLES`):
  probe time minus `max(committed_at)` of `lake_ro.gold."<mart>$snapshots"`, counting only snapshots whose
  `operation` is not `replace`. The daily Iceberg maintenance (`optimize`, `optimize_manifests`) commits `replace`
  snapshots that change no rows; counting them would make gold look refreshed every day at 03:30 while dbt is
  stopped. (`_source_ts_ms` is the wrong clock here: a mart can be rebuilt from old data and still be fresh.)
- **Bad** when any mart is 90 minutes old or more (the next hourly run is due 60 minutes after the last commit, so
  one missed run plus a 30-minute margin), when a mart's probe fails
  (the age sample is then removed), or when the exporter is absent (`or on() vector(1)`). A blind SLI never looks
  green.
- **Not counted** without profile `batch` (namespace `airflow` absent), during its first 2 hours (the first hourly
  run has not built gold yet), and during the first 15 minutes after the exporter (re)starts.

## Alerting

Sloth's multi-window burn alerts (`GoldFreshnessBurn`): page at 1h/5m or 6h/30m, ticket at 1d/2h or 3d/6h, with the
28-day windows of [`slo/windows/shopflow-28d.yaml`](../../slo/windows/shopflow-28d.yaml). Unit-tested:

- Airflow paused right after a run: stale at +90 minutes, page by +105 minutes (silent at +95). So pausing Airflow
  for 2 hours always pages, whether or not the shop has traffic.
- An hourly run keeps gold green for days; no samples and no alert without profile `batch` or while the exporter
  starts; a stale mart, a failing probe and an absent exporter each give bad minutes.
