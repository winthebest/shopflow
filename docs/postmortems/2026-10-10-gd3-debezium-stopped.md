# Postmortem: CDC paused for 30 minutes, no data lost (game day 3: Kafka Connect `cdc` down)

- Date / time window: 2026-10-10 19:15–20:53 (UTC+7); experiment 19:48:01–20:19:09
- Environment: k3d `sf-app` (handed over by sf-app after the ops slot), pin `e530e7c`, profiles
  core,obs-lite,data,ops,chaos; load k6 10 checkouts/s from 19:15:44 to 20:50:44
- Type: game day 3 (planned)
- Severity: page (`CdcLagBurn`) and tickets (`CdcLagBurn`, `PostgresSlotInactive`, `CdcConnectorFailed`); no WAL alert
- Status: action items in review (#200, #201)

## Game day plan (planned experiments only; written before the run)

| | |
|---|---|
| Hypothesis | With the source connector stopped, `debezium_shop` keeps WAL and `PostgresSlotInactive` (ticket) fires ~10–11 min after the stop. bronze.heartbeat is stale after 5 min and `CdcLagBurn` pages ~9 bad minutes later (≈ +14 min; earlier if the cdc SLI has less than ~2h of history). `PostgresSlotWALRetainedHigh` fires only if WAL grows ≥ ~0.57 MiB/s (50% of 2048MB in 30 min); otherwise the measured rate gives the time to the cap, next to the unit-tested rows (1 MiB/s → page at 14 min, 0.1 MiB/s → ticket at ~3h). After Connect returns the connector resumes from its offset: same epoch, no re-snapshot. Secondary (profile ops): fulfillment-worker gets no new orders for 30 min, then KEDA scales it on the backlog. |
| Experiment | `chaos/gd3-debezium-stopped.yaml` (PodChaos `pod-failure` on Kafka Connect `cdc`, 30 min), after 32 min of steady load |
| Blast radius | The CDC pipeline and, with profile ops, fulfillment (shipments delayed, not lost). Checkout must keep working; brokers, shop-db and observability untouched. |
| Stop conditions | Retained WAL over 80% of `max_slot_wal_keep_size`; checkout errors; anything outside kafka/lakehouse degrading. None was met. |
| Measurement window | 19:16–20:53, announced to the orchestrator; no other Docker work; `caffeinate -dims`; `pmset -g log` shows no sleep in the window. |

## Summary

Kafka Connect was down for 30 minutes under a steady 10 checkouts/s. Checkout was unaffected (0 failed requests,
p99 88 ms). The CDC alerts fired as designed: a ticket at +6 min, the page at +8 min, the slot and connector tickets at
+10–11 min. The slot held up to 522 MB of WAL (24% of the cap), far from invalidation, so the WAL alerts stayed
silent, as predicted for that rate. When Connect came back, Debezium resumed from its offset: bronze was fresh
again 1.5 min later, the WAL went back to 17 MB, the worker drained a 34,845-record backlog in 131 s, and no order was
lost or shipped twice.

## Impact

| | Value | Source |
|---|---|---|
| Checkout during the session | 114,001 HTTP requests (57,000 iterations), 0 failed; p95 73 ms, p99 88 ms | k6 summary |
| Checkout SLIs during the experiment | 0 bad events (5m error ratio 0 for availability and latency) | `checkout-*-error-5m.csv` |
| Bronze stale (heartbeat ≥ 5 min) | 28 bad minutes of the cdc-lag SLI | `cdc-stale-minute.csv` |
| cdc-lag error budget used | 28 of the 28-day budget's ~403 minutes (1% of 40,320) ≈ 7%. Grafana showed −2,879% because this cluster's SLI held only ~2h of data | `error-budget-remaining.csv` |
| Peak WAL held by the slot | 522,072,120 bytes (24.3% of 2048 MB) at 20:19:44 | `wal-retained-bytes.csv` |
| Fulfillment | no shipment for 30 min; backlog 34,845 records drained in 131 s; 59,474 shipments, 0 duplicates | sf-app `app-worker-check` (watch) |

## Timeline (UTC+7)

| Time | Event |
|---|---|
| 19:15:44 | k6 10/s starts; Chaos Mesh installed (`make sre-chaos-on`) at 19:17 |
| 19:15–19:32 | `CheckoutLatencyBurn` page and checkout tickets already firing: left over from sf-app's 80/s overload test in the 1h/6h windows (5m windows at 0); the page cleared at 19:32 |
| 19:48:01 | `kubectl apply -f chaos/gd3-debezium-stopped.yaml`; Connect pod 0/1, slot inactive by 19:48:33 |
| 19:54:14 | `CdcLagBurn` ticket (+6m13s; heartbeat age 387 s) |
| 19:56:14 | `CdcLagBurn` **page** (+8m13s) |
| 19:58:29 | `PostgresSlotInactive` ticket (+10m28s); `TargetDown` (Connect metrics) |
| 19:58:44 | `CdcConnectorFailed` ticket for both connectors (+10m43s) |
| 20:03:29 / 20:11:29 | `KubePodNotReady`, `KubePodCrashLooping` on `cdc-connect-0` (how `pod-failure` works: the container is swapped for a pause image) |
| 20:15:10 | KEDA watch started (sf-app's `app-worker-check WATCH=1`) |
| 20:19:09 | Chaos Mesh restores Connect (`duration` reached); `cdc-connect-0` 1/1; slot and connector tickets resolve by 20:18–20:19 |
| 20:19:38 | fulfillment-worker at 3 replicas (7 s after the backlog appeared) |
| 20:20:40 | bronze.heartbeat fresh again (age 60 s): CDC caught up 1.5 min after Connect returned |
| 20:22 | backlog drained (131 s), worker back to 1 replica (185 s) |
| 20:23:39 | WAL held by the slot back to 17 MB |
| 20:50:30 | `CdcLagBurn` page resolved: the 6h/30m pair needs its 30m window clean (≈ 30 min after the last bad minute) |
| 20:51–20:53 | evidence exported; `make sre-chaos-off` failed (CRDs left), CRDs deleted, re-run PASS |

## Root cause and contributing factors

1. Planned: Kafka Connect `cdc` unavailable for 30 minutes (Chaos Mesh `pod-failure`).
2. The slot keeps WAL while nothing confirms it: 17 MB → 520 MB in 30 min, 0.266 MiB/s, which is one 16 MiB segment a
   minute. With `archive_timeout: 60s` and the CDC heartbeat writing every 10 s, Postgres switches segment every
   minute, and a switch moves the LSN to the next segment boundary, so the slot holds a full segment per minute
   whatever the checkout volume. Time from a stopped connector to the 2048MB cap is therefore about 2 hours on any
   cluster with profile data (more only under heavier writes).

## Result vs hypothesis

| Hypothesis | Result |
|---|---|
| `PostgresSlotInactive` ~10–11 min | **Matched**: +10m28s |
| `CdcLagBurn` pages ~+14 min, earlier with short history | **Matched**: page at +8m13s, through the 6h/30m pair after 3–4 bad minutes. That only happens when the 6h window holds under ~65 counted minutes: the cdc SLI on this cluster had about an hour of history |
| WAL alerts only at ≥ ~0.57 MiB/s | **Matched**: 0.266 MiB/s, peak 24.3%, no WAL alert. At this rate, had the stop gone on: `PostgresSlotWALRetainedHigh` ~+68 min, `…Critical` (1h projection past the cap, over 25%) ~+72 min, slot invalidated ~+127 min. So the page leaves ~55 min, between the unit-tested 20 min (1 MiB/s) and ~1 h (0.1 MiB/s). The thresholds are capacity-based, so lead time grows as the rate falls |
| Resume from offset, no re-snapshot | **Matched**: same epoch, bronze fresh 1.5 min after Connect returned, both connectors RUNNING |
| Fulfillment pauses, KEDA scales on the backlog | **Matched**: 1→3 replicas in 7 s, 34,845 records drained in 131 s at ~100 records/s per replica, 0 duplicates. The check "every paid order has a shipment" reported 2 unshipped orders: newest orders still in flight under load (re-checked: 0 older than 60 s); sf-app added a 60 s grace in #190 |

## What went well

- Every planned alert fired in the predicted order; the page came from the SLO, not from a symptom alert.
- Nothing was lost: the logical slot and Connect's offsets carried the 30 minutes over; no re-snapshot.
- The game-day runner (inject, KEDA watch at +27 min, recovery checks) and `sre-gameday-evidence` captured exact
  alert times without anyone watching a dashboard at the right second.

## What went wrong

- `make sre-chaos-off` failed its own check: Argo CD applies a Helm chart's `crds/` without tracking them, so
  deleting the `chaos-mesh` Application left 23 `chaos-mesh.org` CRDs (no instances, no webhooks). Deleted by hand
  after checking there were no instances; the re-run passed.
- The Prometheus port-forward helper leaked one `kubectl port-forward` per run (67 by the end): it started the
  process through a shell-function wrapper, so the exit trap killed a subshell, not kubectl. Found by sf-app; the
  orphans were killed at 20:53.
- Symptom alerts (`TargetDown`, `KubePodNotReady`, `KubePodCrashLooping`) duplicated the CDC tickets for a planned
  Connect outage. Acceptable in a game day; a real outage would want them inhibited by `CdcConnectorFailed`.
- The checkout page from sf-app's earlier overload test was still firing at the start; a game day on a reused
  cluster should start from a clean alert list or note it, as done here.

## Action items

| Action | Owner | Status |
|---|---|---|
| `sre-chaos-off` deletes the `chaos-mesh.org` CRDs explicitly | sf-sre | PR #201 |
| Port-forward helper runs kubectl directly in the background; trap kills and waits | sf-sre | PR #200 |
| `app-worker-check`: paid-without-shipment counts only orders older than 60 s; KEDA `lagThreshold` 50 → 3000 | sf-app | PR #190 |
| Decide whether to prove the WAL alerts live with a lower cap (256MB on a game-day branch, new cluster), now that the rate is known (0.266 MiB/s → High at ~128MB in ~8 min, page soon after) | sf-sre + orchestrator | proposed |
| Consider inhibiting `KubePodNotReady`/`TargetDown` for Connect while `CdcConnectorFailed` fires | sf-sre | open |
