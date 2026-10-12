# Postmortem: Game day 3b — the WAL alerts fire live before the slot is lost (scenario 3 re-run, 512MB cap)

- Date / time window: 2026-10-12 11:49–12:40 (UTC+7); experiment 12:12:07–12:34:40
- Environment: fresh k3d `sf-sre`, profiles core,obs-lite,data,chaos; revision `4e6b772` (branch
  `exp/gd3b-slot-cap-512mb`, not for merge: main plus `max_slot_wal_keep_size: 512MB`); k6 10 checkouts/s
- Type: game day 3, second run (same scenario as [game day 3](2026-10-10-gd3-debezium-stopped.md); the plan counts
  scenarios, not runs)
- Severity: pages (`PostgresSlotWALRetainedCritical`, `CdcLagBurn`) and tickets; slot never invalidated
- Status: action items in review (#225)

## Why a second run, and what differs from game day 3

Game day 3 showed that the slot holds ~16 MiB of WAL a minute, so with the 2048MB cap the WAL alerts could not fire in
a 30-minute stop (peak 24%). They were proven only by unit tests. This run lowers the cap to 512MB on a branch that is
never merged, on a fresh cluster, so that the same rate crosses the alert thresholds within the experiment:

| | Game day 3 | Game day 3b |
|---|---|---|
| `max_slot_wal_keep_size` | 2048MB (main) | **512MB** (game-day branch) |
| Experiment | 30 min, ops profile up | **22 min**, stop automatically at 80% of the cap |
| Goal | CDC alerts, recovery, KEDA backlog | **see `PostgresSlotWALRetainedCritical` / `High` fire live** before invalidation |

## Game day plan (written before the run)

| | |
|---|---|
| Hypothesis | At ~16 MiB/min the slot passes 25% of 512MB at ~+8 min and the Critical page (1-hour projection past the cap) fires at ~+13 min; `High` (50%) at ~+21 min; invalidation would come at ~+32 min. The connector resumes from its offset, no re-snapshot. |
| Experiment | `chaos/gd3-debezium-stopped.yaml` with `duration: 22m` (applied through `yq`, the file unchanged) after 15 min of steady load |
| Blast radius | CDC (Kafka Connect `cdc`); checkout must keep working |
| Stop conditions | Automatic: the runner deletes the PodChaos if the slot holds ≥ 80% of the cap (checked every 30 s) |
| Measurement window | 11:49–12:41, announced; `caffeinate -dims`; `pmset -g log`: no sleep |

## Summary

Both WAL alerts fired on the cluster while the slot was still far from the cap: the Critical page at +18 min (56% of
the cap) and the `High` ticket at +21.5 min. The page came ~5 minutes later than planned because its 1-hour projection uses a
30-minute `predict_linear` window that still held the flat minutes before the stop. After the experiment the slot kept
growing for 2.5 minutes until Debezium's first offset flush and peaked at 78%, just under the 80% stop line. CDC
resumed from its offset, bronze was fresh 1.5 minutes after the stop ended, and nothing was lost.

## Impact

| | Value | Source |
|---|---|---|
| Checkout during the experiment (12:12–12:40) | 16,258 × 201, 13 × 504 (12 at 12:22, 1 at 12:36) | `checkout-by-status-per-minute.csv` |
| Bronze stale (cdc-lag SLI) | 18 bad minutes | `cdc-stale-minute.csv` |
| Peak WAL held by the slot | 418 MB = 77.9% of 512MB at 12:36:44, 2.5 min after Connect returned | raw `cnpg_pg_replication_slots_pg_wal_lsn_diff` |
| WAL growth while stopped | 16.7 MB/min (one segment a minute) | `samples.tsv` |

## Timeline (UTC+7)

| Time | Event |
|---|---|
| 11:49:28–11:56:13 | `make up` on a fresh cluster (see *`make up` records the CDC snapshot* below) |
| 11:56:31 | Chaos Mesh healthy; k6 10/s from 11:57 |
| 12:05:05–12:05:20 | **Unrelated**: a ~15 s host/VM stall. Every container spiked CPU at once, the WAL archive took 6.8 s, Postgres reset SSL handshakes, orders' pool timed out → 319 checkout 5xx in that minute; `CheckoutAvailabilityBurn` page 12:06:45–12:09:30 (sf-app's diagnosis; clock jump ruled out) |
| 12:12:07 | gd3 applied (22 min) |
| 12:13 | slot `debezium_shop` inactive |
| 12:19:00 | `CdcLagBurn` page and ticket (+6m53s) |
| 12:22:15 | `CdcConnectorFailed` (+10m08s) |
| 12:23:45 | `PostgresSlotInactive` (+11m38s) |
| **12:30:15** | **`PostgresSlotWALRetainedCritical` page (+18m08s), 56% of the cap** |
| **12:33:45** | **`PostgresSlotWALRetainedHigh` ticket (+21m38s)** |
| 12:34:40 | experiment over (duration); the 80% stop never triggered |
| 12:35:47 | slot active again, both connectors RUNNING |
| 12:36:10 | bronze fresh again (heartbeat 28 s) |
| 12:36:44 | slot peak 77.9%; 12:37:25 down to 6% after Debezium's first offset flush |
| 12:37:30 | both WAL alerts resolved |
| 12:38–12:40 | evidence exported; `make sre-chaos-off` PASS on the first run (CRD step from #201); `make down` |

## Result vs hypothesis

| Hypothesis | Result |
|---|---|
| Critical page ~+13 min | **Later: +18m08s.** The 1-hour projection uses `predict_linear(...[30m])`; right after a stop most of the window still holds the flat minutes before it, which drags the slope down. promtool replay: the page comes at +17 min with 30m and at +11 min with 15m (#225) |
| `High` ticket ~+21 min | **Matched**: +21m38s, before Connect returned |
| No invalidation | **Matched**: peak 77.9%. The peak came *after* the stop: the slot only moves at Debezium's offset flush once it has caught up |
| Resume from offset, no re-snapshot | **Matched**: same epoch, bronze fresh 1.5 min after the stop |

## `make up` records the CDC snapshot (evidence for #189)

The fresh `make up` waited for the CDC snapshot and recorded it, so silver and gold are not empty after a fresh start:
`ready in 405s (apps 337s, CDC snapshot 68s)`; `meta.cdc_epochs` had one row, epoch 1791780629, `started_at`
11:55:07, `snapshot_completed_at` 11:56:13 (UTC+7). Source: `make-up.log`, `meta-cdc-epochs.txt`.

## What went well

- The WAL alerts did their job live, with the slot at 56% (page) and ~66% (ticket), long before invalidation.
- The automatic stop, the duration override and the evidence export made the run hands-off; `sre-chaos-off` passed
  first time with the CRD fix.
- A short host stall right before the experiment was diagnosed within minutes (sf-app) and kept out of the results.

## What went wrong

- The plan's +13 min for the page ignored the `predict_linear` window lag; the page came 5 minutes later.
- The 80% stop line is checked only during the experiment, while the slot keeps growing ~2.5 min after it (to 77.9%
  here). A stop based on retained WAL must leave room for that growth.
- 12 × 504 at 12:22 during the experiment: gateway → orders past 1 s (no payments timeouts, no pool errors). Checkout
  does not use CDC; most likely another short host hiccup. **Under investigation, not attributed to the experiment.**
- 1 × 504 at 12:36, while Debezium decoded ~385 MB of backlog on the primary that checkout shares: a real side effect
  of the CDC catch-up (observation, not a failure).

## Action items

| Action | Owner | Status |
|---|---|---|
| Critical page: `predict_linear` window 30m → 15m, with replay and no-false-positive promtool tests | sf-sre | PR #225 |
| Runbooks: retained WAL keeps growing 2–3 min after the connector returns, and the catch-up loads the primary; WAL-based stop at retained + rate × 3 min ≥ 80% (≈ 70% for 512MB) | sf-sre | PR #225 |
| Host sampler (load average, top CPU every 10 s) in the next app slot, to catch short host stalls | sf-app | planned |
| Delete branch `exp/gd3b-slot-cap-512mb` once this postmortem is merged | sf-platform | open |
