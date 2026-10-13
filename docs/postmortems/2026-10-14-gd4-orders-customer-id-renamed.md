# Postmortem: Game day 4 — orders.customer_id renamed outside CI, caught by the hourly dbt run after 65 minutes

- **Detection as deployed at `9fa67e1`: never.** The shop-db exporter query behind `AirflowDagRunFailed` gets
  `permission denied for table dag_run`. There is no series, so the alert cannot fire for any DAG (see What went wrong).
  This run measured the path with a one-off grant on `dag_run`, added by hand before the inject (04:30:06).
- **Detection is not notification.** The alert fired in Prometheus and Alertmanager. Alertmanager could not deliver it,
  because the local Discord webhook is a placeholder. Time to a human was not measured locally: the 65 minutes below are
  when the alert fired, not when someone was told.
- Run (UTC+7, 2026-10-14): 04:31:55–06:17:38. T0 = 05:05:57, the inject's commit (2026-10-13 22:05:57Z). Revision
  `9fa67e1` plus the one-off grant.
- Environment: k3d `sf-data`, inside sf-data's Phase 5 slot. Profiles core,obs-lite,data,batch. k6 sent 2 checkouts/s
  (POST /checkout only) with `RETRIES=0`. Nothing else wrote to the cluster during the run.
- Type: game day 4 (scenario 4), simulated: the DDL was applied directly instead of shipping a release (see below).
- Severity: ticket (`AirflowDagRunFailed{dag_id="dbt_build"}`). Checkout SLOs were unaffected.
- Status: action items open
- Validity:
  - 0 host sleeps (`pmset`); host guard ok before the inject, before the restore and at the end;
  - table `orders` dumped before the inject;
  - evidence in `out/gd4-20261013T213155Z`, not in git (`SHA256SUMS`, sha256 `1ba63f8a…aa94`).

## What was simulated, and why this way

The scenario is a release whose migration and ORM renamed `orders.customer_id`, with `data/contracts` and silver left
unchanged. The data-contract gate in CI refuses that change ([ADR 0101](../adr/0101-data-contracts-in-ci.md)), so
the simulation applies the DDL directly: `chaos/gd4-orders-customer-id-renamed.sql`.

A plain rename would break checkout first, because the orders ORM inserts every mapped column. So a nullable
`customer_id` shim column and a BEFORE INSERT trigger keep the running app writing, while the value moves into
`buyer_id`. Every existing row's shim is NULL too, so an update of an older order also carries `customer_id = null`.
Downstream, the signal matches a real rename: the sink (schema evolution off) drops `buyer_id` and writes
`customer_id = null`.

A real release would be more faithful. It would need an orders image built from a branch with the migration, pushed
to the cluster's local registry and pinned by digest. That costs a Docker build in its own slot for the same downstream
signal.

## Game day plan (written before the run)

| | |
|---|---|
| Hypothesis | Checkout stays green. From T0, every order inserted or updated reaches bronze with `customer_id = null`. Silver `orders`' `not_null` test fails at the next hourly `dbt_build`, after one retry 5 minutes later, and `AirflowDagRunFailed` (`for: 5m`) fires. Detection = time to the next run + ~10 min; worst case ~70 min if T0 falls just after a run. |
| Experiment | `chaos/gd4-orders-customer-id-renamed.sql`: one transaction, `lock_timeout` 2s. T0 comes 5 min after a scheduled `dbt_build` ends green. |
| What counts as detection | The scheduled run fails with `not_null` on silver `orders.customer_id` among its failing tests, and the alert fires. A failing `relationships` test on `order_items` alone does not count: it fails now and then under traffic. |
| Blast radius | The data pipeline from bronze `orders` on. `GET /orders/{id}` and replays of settled orders answer 500 from orders (502 at the gateway), which k6 does not call. Checkout must not degrade. |
| Restore | After detection, or after 95 min without it: `chaos/gd4-orders-customer-id-restored.sql` with T0. It undoes the DDL in one transaction, then touches every order updated since T0 − 1 min so that CDC sends it again. Recovery = the next green `dbt_build`. |
| Measurement window | Exclusive: nothing else wrote to the cluster, and no heavy Docker work ran on the host. The host guard ran before the inject, before the restore and at the end. |

## Summary

Every order written after the rename reached the pipeline without a customer. Checkout was unaffected. The next
scheduled `dbt_build`, 54 minutes later, failed on silver's `not_null` test for `orders.customer_id` (6,479 orders),
on both tries. `AirflowDagRunFailed` fired 65m09s after T0, as predicted. That only happened because a grant was added
by hand: as deployed, the alert cannot fire. The restore took one second. A run triggered by hand went green 2m40s
later, and the alert resolved within 4 minutes of the restore.

Silver `orders` held the NULLs from 06:00 until that run. Gold was never rebuilt from them and only aged, to 72
minutes at most.

## Detection

| Step | Time (UTC+7) | Since T0 | Source |
|---|---|---|---|
| Inject committed (T0) | 05:05:57 | 0 | `inject.out` |
| Orders written since T0 have `customer_id` NULL in Postgres | 05:06:27: 61 of 61 | +30 s | `timeline.log` |
| Next scheduled `dbt_build` starts (`scheduled__2026-10-13T23:00:00+00:00`) | 06:00:00 | +54m03s | Airflow `dag_run` |
| `not_null_orders_customer_id` fails, try 1: 6,479 rows, the only failure (PASS=7 ERROR=1) | 06:00:16 | +54m19s | task log (sf-data) |
| The same test fails on the retry, same 6,479 rows | 06:05:20 | +59m23s | task log (sf-data) |
| Run ends `failed` | 06:05:26 | +59m29s | Airflow `dag_run` |
| `AirflowDagRunFailed{dag_id="dbt_build"}` pending | 06:06:06 | +60m09s | Prometheus, 15 s step |
| `AirflowDagRunFailed{dag_id="dbt_build"}` firing | 06:11:06 | **+65m09s** (3,909 s) | Prometheus, 15 s step |

Detection = the first Prometheus sample with the alert firing, minus T0. The runner polls every 20 s and saw it
firing at 06:11:08 (3,911 s).

The 65 minutes split into three parts:

| Part | Window | Duration |
|---|---|---|
| Waiting for the hourly schedule | T0 → 06:00:00 | 54m03s |
| The run with its retry (`retries: 1`, `retry_delay` 5 min) | → 06:05:26 | 5m26s |
| The alert | → 06:11:06 | 5m39s |

The alert part is `for: 5m` (`deploy/platform/slo/base/airflow-dag-runs.prometheusrule.yaml`), plus about 40 s for the
exporter scrape and the rule evaluation.

With the alert working, the worst case is one schedule interval + the run with its retry + ~5m40s, about 71 minutes.
The `relationships` test on `order_items`, which fails now and then under traffic, did not run: it and all of gold were
`upstream_failed`.

## Restore and recovery

| Step | Time (UTC+7) | Since the restore | Source |
|---|---|---|---|
| Restore committed (runner, right after detection) | 06:11:11 | 0 | `timeline.log` |
| Checks after the restore | 06:11:11 | | `timeline.log` |
| `dbt_build` triggered by hand (`manual__2026-10-13T23:12:57.603179+00:00`), after two Iceberg sink commits | 06:12:57 | +1m46s | sf-data |
| That run ends green: every task passed on the first try | 06:13:51 | +2m40s | Airflow `dag_run` |
| Oldest gold refresh 29 s, down from 4,348 s at 06:13:33 | 06:14:34 | +3m23s | `samples.tsv` |
| `AirflowDagRunFailed{dag_id="dbt_build"}` resolved (last sample firing / first sample without it) | 06:14:55 / 06:15:06 | ≤ +3m55s | Prometheus |
| All six gold marts refreshed within the last 151 s | 06:17:01 | +5m50s | `timeline.log` |

The checks after the restore:
- 0 orders without a customer;
- column `customer_id` is back, and the shim, trigger and function are gone;
- 7,948 rows touched;
- the checksum of the orders dumped before the inject is unchanged.

Recovery is labelled manual. Without the trigger, the first run to read the restored rows would have been the 07:00
scheduled run.

The two row counts measure different things:
- **6,479** is what silver counted at 06:00: orders whose latest change, as of that run's CDC epoch, had no customer.
  That matches 54 minutes at ~2 orders/s.
- **7,948** is what the restore touched at 06:11:11: its predicate is `updated_at >= T0 − 1 minute`. That covers 11
  more minutes of traffic plus a minute of margin for checkouts whose transaction began before the rename.

Just before the restore, Postgres had 7,749 orders written since T0, all without a customer (06:10:31). 21 s after the
restore, it had 0 of 7,991.

## Impact

| | Value | Source |
|---|---|---|
| Checkout, availability and latency | 5m and 1h burn 0 throughout; p99 0.10–0.21 s; ~2 requests/s | `checkout-burn-*.csv`, `checkout-latency-p99-s.csv`, `checkout-requests-per-s.csv` |
| Silver `orders` | Rebuilt at 06:00 with `customer_id` NULL on 6,479 orders, because the DAG builds each model and then tests it. Correct again at 06:13:51. | `data/airflow/dags/dbt_build.py`, task logs |
| Gold | Not rebuilt at 06:00 (`upstream_failed`): its data stayed correct but went stale. The oldest mart refresh reached 72 min (4,348 s at 06:13:33), under the 90-minute gold-freshness threshold. | `samples.tsv` |
| Postgres `orders` | No order lost. The orders written before the inject are unchanged (checksum), and every order since T0 got its `customer_id` back from `buyer_id`. | `timeline.log` |

## Result vs hypothesis

| Prediction | Measured | |
|---|---|---|
| Checkout stays green | burn 0 throughout | as predicted |
| From T0, every order written reaches the pipeline without a customer | 100% of orders written since T0, in every sample (7,749 of 7,749 at 06:10:31) | as predicted (checked in Postgres and by silver's count; bronze was not queried) |
| Silver's `not_null` fails at the next hourly run, after one retry | failed on both tries, and it was the only failing test | as predicted |
| Detection = time to the next run + ~10 min | 54m03s + 11m06s | as predicted, about a minute longer |
| `AirflowDagRunFailed` can fire as deployed | it cannot: `permission denied for table dag_run` | not predicted; found before the inject |
| Restore, then the next `dbt_build` is green | green 2m40s after the restore, on a run triggered by hand | as predicted |

## What went well

- Silver's `not_null` on `customer_id` ([#237](https://github.com/winthebest/shopflow/pull/237)) caught the change on
  the first scheduled run, and the failing test names the column. Nothing downstream of silver `orders` ran, so gold
  never published orders without a customer.
- sf-data's checks before the run found the broken alert, so the run measured the detection path instead of waiting
  95 minutes for an alert that could not fire.
- The restore was clean: it took one second, left 0 orders without a customer, and the dumped orders matched their
  checksum.
- Checkout did not notice the shim: burn stayed at 0, and p99 stayed near 0.1 s.

## What went wrong

- **The alert could not fire as deployed.**
  - The shop-db custom query `airflow_dag_run` runs as `cnpg_metrics_exporter` (a `pg_monitor` member), which has no
    privilege on Airflow's `dag_run`. With no series, `AirflowDagRunFailed` cannot fire for any DAG.
  - The collector's own error gauge (`cnpg_collector_last_collection_error`) stayed 0. Only `cnpg_errors_total` showed
    the failure. It had one series per failing query, labelled `errorUserQueries="airflow_dag_run on db airflow:
    ERROR: permission denied for table dag_run (SQLSTATE 42501)"`.
    - The value stayed 1 from 04:09:17 until the grant, and the series disappeared after it.
    - Despite its name it does not count up, so `increase()` over it stays 0.
  - The rule's promtool tests supply the series themselves, so they cannot notice a query that never produces one.
  - The durable fix is a grant in Airflow's migration Job. [#265](https://github.com/winthebest/shopflow/pull/265)
    (open) documents it on the query.
- **No notification was delivered.** `AlertmanagerFailedToSendAlerts` and `AlertmanagerClusterFailedToSendAlerts` were
  firing from 05:12:10, before T0, until the end. Their `discord` integration points at the placeholder webhook.
- **Detection waits for the schedule.** 54 of the 65 minutes were spent waiting for the next hourly run. A change made
  just after a run is found about 71 minutes later.

## Prevention

The `contracts` job in CI ([ADR 0101](../adr/0101-data-contracts-in-ci.md), `scripts/check_contracts.py`) fails this
rename, because it changes the `orders` contract without a version bump. Two gaps remain:
- No test shows that the gate rejects it.
- The same rename with a version bump passes the gate. Silver would then still break at the next hourly run, unless the
  same change updates it. The bump makes the break visible in review, but nothing enforces the consumer side.

## Other alerts during the run (not GD4)

- `CdcLagBurn` (ticket), 05:43:10–06:04:55: Polaris was OOM-killed at 05:41:06. The Iceberg sink's connection was
  refused, so the sink task failed, and Strimzi restarted it at 05:42:44. No commit landed from 05:41:05 until 05:43:49.
  - While Polaris was down, the freshness probe could not read bronze: there is no heartbeat-age sample at 05:41:55.
    That failed probe is the one stale minute (05:42:55). The heartbeat age peaked at 140 s, far under the 5-minute
    threshold.
  - The connect log from 05:04 to 05:50 has no warning or retry from the rename (sf-data).
  - Before 05:41, the sink committed every 60–61 s. Heartbeat ages of ~64 s (05:22:55–05:33:55) are that interval
    sampled just before a commit, not lag.
  - sf-data follows it up as a Phase 5 capacity finding.
- `AirflowDagRunFailed{dag_id="reconciliation"}`, firing from 04:36:10 to the end: the catch-up run at the cluster's
  start failed, before T0.
- `AlertmanagerFailedToSendAlerts`, `AlertmanagerClusterFailedToSendAlerts`: the placeholder webhook (What went wrong).

## Open questions

- **A breaking change with a version bump.** Should the consumer side be checked too? For example, a PR that bumps a
  contract's version could be required to touch silver.

## Action items

| Action | Owner | Status |
|---|---|---|
| Durable grant on `dag_run` (`dag_id`, `state`, `end_date`) to `pg_monitor`, in Airflow's migration Job right after `airflow db migrate`; the shop-db query documents it | sf-data; sf-platform ([#265](https://github.com/winthebest/shopflow/pull/265)) | open |
| Live proof of the grant on a fresh cluster: the `dag_run` series is present without a grant by hand | sf-data, sf-sre (next batch slot) | open |
| Alert while any `cnpg_errors_total` series exists (`for` long enough to skip the bring-up error before Airflow's database exists), naming the query from `errorUserQueries`. The gate check fails when any such series is present, and checks that the `dag_run` series exists when profile batch is up. Both get a fake-tool test like #260's | sf-sre | open |
| A test that the contract gate rejects this rename: `orders.customer_id` renamed without a version bump fails `check_contracts.py` | sf-app | open |
| Alertmanager delivery: the local webhook is a placeholder, so no game day has delivered a notification. A delivery test needs the real webhook | user decision | open |
| `not_null` on silver `orders.total` and `orders.status` (found while preparing: `accepted_values` passes NULLs) | sf-data | done ([#237](https://github.com/winthebest/shopflow/pull/237)) |
| Candidate: a schema-drift check every few minutes (Postgres `information_schema` vs `data/contracts`), to detect in minutes instead of at the next hourly run | sf-data, sf-sre | open: decide |
