# SLO: gold freshness

Owner: sf-data (sf-sre reviews). Spec: [`slo/gold-freshness.yaml`](../../slo/gold-freshness.yaml) → generated rules
[`deploy/platform/slo/base/gold-freshness-slo.prometheusrule.yaml`](../../deploy/platform/slo/base/gold-freshness-slo.prometheusrule.yaml)
(Sloth, [ADR 0300](../adr/0300-slo-tooling-sloth.md)). SLI rule:
[`gold-freshness-sli.prometheusrule.yaml`](../../deploy/platform/slo/base/gold-freshness-sli.prometheusrule.yaml).
Tests: [`slo/tests/gold-freshness.test.yaml`](../../slo/tests/gold-freshness.test.yaml). Runbook:
[gold-stale](../runbooks/gold-stale.md).

## Why gold

The gold marts (`lake.gold.*`: `fct_orders`, `dim_customers`, `mart_daily_revenue`, `mart_payment_success_rate`,
`mart_customer_ltv`, `mart_product_sales`) are what Metabase shows. dbt rebuilds them every hour (Airflow DAG `dbt_build`, profile `batch`;
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
  `fct_orders` and `dim_customers` must also hold rows whenever bronze `orders` / `customers` do
  (`REFRESH_ROWS_FROM`): empty while their source is not, their probe fails. The other marts filter or aggregate
  (paid orders only) and can be empty legitimately (no orders yet, nothing paid): their commit alone counts.
- **Bad** when any mart is 90 minutes old or more (the next hourly run is due 60 minutes after the last commit, so
  one missed run plus a 30-minute margin), when no probe of a mart has succeeded for 3 minutes
  (`freshness_last_success_timestamp`; a failed probe removes the age sample, and this includes `fct_orders` or
  `dim_customers` empty while their bronze source has rows), when a mart's probe has never succeeded, or when the
  exporter is absent (`or on() vector(1)`). A blind SLI never looks green, but one failed probe round is not a bad
  minute. The probes read gold through Polaris, whose catalog database is shop-db, so a shop-db failover fails them for
  a round while gold is fresh. As for `cdc-lag`, a round that fails fast stays visible for about 160 s: 180 s
  tolerates up to two such rounds, and three always count.
- **Not counted** without profile `batch` (namespace `airflow` absent), during its first 2 hours (the first hourly
  run has not built gold yet), and during the first 15 minutes after the exporter (re)starts.
- **Empty marts:** a commit alone is not freshness. dbt rewrites every mart each hour (CREATE OR REPLACE, `append`
  snapshots) even when silver is empty. Seen on 2026-10-10: on a fresh cluster nothing recorded the CDC epoch in
  `meta.cdc_epochs`, so silver joined no epoch, and gold would have been empty yet "fresh". `make up` now waits for
  the epoch's snapshot (`cdc-epoch.sh wait`), the dbt test `assert_completed_cdc_epoch_exists` fails such a run
  (`AirflowDagRunFailed` raises a ticket), and `fct_orders` or `dim_customers` empty while bronze has rows now counts
  as bad minutes here, from 3 minutes after the mart's last successful probe. A shop with no orders yet, or with nothing paid, stays green.

## Alerting

Sloth's multi-window burn alerts (`GoldFreshnessBurn`): page at 1h/5m or 6h/30m, ticket at 1d/2h or 3d/6h, with the
28-day windows of [`slo/windows/shopflow-28d.yaml`](../../slo/windows/shopflow-28d.yaml). Unit-tested:

- Airflow paused right after a run: stale at +90 minutes, page by +105 minutes (silent at +95). So pausing Airflow
  for 2 hours always pages, whether or not the shop has traffic.
- An hourly run keeps gold green for days; no samples and no alert without profile `batch` or while the exporter
  starts; a stale mart, a probe failing for 3 minutes, a mart whose probe never succeeded and an absent exporter
  each give bad minutes. One failed probe round on a young cluster neither counts nor alerts.

On a fresh cluster, the 6h, 1d and 3d windows hold only the SLI's short history, which starts 2 hours after
namespace `airflow` appears. So the ticket fires on the first bad minute (3d/6h above 1%), and the 6h/30m page fires
after a few bad minutes. Only the 1h/5m page leg behaves as it would with a full history: it fires after 9 bad
minutes. Measured once on a cluster (ADR 0414): the 1h/5m leg crossed about 99 minutes after the last refresh.
