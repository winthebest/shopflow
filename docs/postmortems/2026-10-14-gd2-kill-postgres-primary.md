# Postmortem: Game day 2 — the shop-db primary killed, without and with logical slot sync

- Status: complete. Both arms have a valid run.
- Runs (UTC+7, 2026-10-14), each on a fresh k3d `sf-sre`:
  - **H-A** (`PG_LOGICAL_SLOT_SYNC=off`): 08:06:57–08:59:36; kill 08:37:22.
  - **H-B** (slot sync on): 11:55:50–12:46:31; kill 12:24:17.
  - H-B's first run (08:59:36) is invalid: the lid was closed on battery 8m46s after the kill and the Mac slept.
- Revision `c0f0691621d706ee55a1586da8082a08c0d05e3f` (main, with #259, #267 and #268), all runs. Runner
  `gd2-run.sh`, sha256 `778bed06…439a`, frozen before the runs.
- Environment: profiles core,obs-lite,data,chaos, `PG_INSTANCES=2`. k6 sent 10 checkouts/s with `RETRIES=0`: 20 min
  before the kill, 20 min after. No other cluster ran on the host.
- Type: game day 2 (scenario 2).
- Severity: ticket. Both runs raised `CheckoutAvailabilityBurn`, and H-B also raised `CheckoutLatencyBurn`. The failover
  itself raised no alert. H-B's `CdcLagBurn` page was false: CDC was on time, the probe failed for one minute, and the
  cluster was young enough for one minute to page (see
  [The CDC page that was not about CDC](#the-cdc-page-that-was-not-about-cdc)).
- Validity:
  - H-A: 0 host sleeps; no Polaris, Trino coordinator or Connect restart within 15 min of the kill; evidence in
    `out/gd2-H-A-20261014T010657Z`.
  - H-B: 0 host sleeps; no such restart; evidence in `out/gd2-H-B-20261014T045550Z`. By user decision it ran on
    battery (89% → 71%) with the lid open. The host guard required an open lid and at least 15% battery before each
    phase.

## H-A vs H-B

| | H-A (no sync) | H-B (sync) | Source |
|---|---|---|---|
| Kill → new target chosen → promoted | +3.3 s → +20.7 s | +2.9 s → +20.6 s | `cluster-status.tsv`, `pg-shop-db-2.log` |
| Killed pod back as a streaming standby | +36 s | +30 s | `pg_stat_replication` on the new primary |
| Longest silence in acked checkouts | 24.9 s (08:37:24.58 → 08:37:49.5) | 23.3 s (12:24:19.35 → 12:24:42.62) | `acks.jsonl` |
| Checkout responses, the failover minute | 201: 443, 502: 159, 504: 102 | 201: 359, 502: 148, 504: 113 | `samples.tsv` |
| Failed requests, whole run (k6) | checkout 225 of 25,190; GET /products 234 | checkout 211 of 25,191; GET /products 220 | `k6-summary.json` |
| Acked orders missing from Postgres (the RPO) | 0 of 24,965 | 0 of 24,980 | ids on the new primary |
| Acked orders in kill ±15 min missing from bronze | 0 of 17,665 | 0 of 17,679 | `cdc-gap/bronze.tsv` |
| `debezium_shop` on the new primary, 60 s after the promotion | `failover t, synced f, active t`: recreated | `failover t, synced t, active t`, confirmed_flush unchanged from the old primary: the synced copy | `slots.txt` |
| `synchronized_standby_slots` (primary → new primary) | empty → empty | `_cnpg_shop_db_2` → `_cnpg_shop_db_1` | `SHOW` on each |
| Connector | slot recreated; snapshot into the same epoch (12,070 `r` rows of orders) | resumed on the synced slot; snapshot skipped, 0 `r` rows | `connect.log`, `cdc-gap/` |
| Hazard window: new primary up → slot ready | 4.07 s, no write in it | none: the slot existed from the promotion. 54 acked orders were written before Debezium resumed, and all of them streamed. | `pg-shop-db-2.log`, acks |
| CDC gap: only in Postgres / KPI misses (`r` without `c`) / stale in silver | 0 / 0 / 0 | 0 / 0 / 0 | `cdc-gap/` |
| Bronze heartbeat age around the kill | 42.9 s → 52.8 s once, then under 49 s | 8.5 s → no sample (failed probe) → 11.0 s, then under 27 s | `cdc-heartbeat-age-s.csv` |
| Alerts | `CheckoutAvailabilityBurn` ticket (+40 s) | `CheckoutAvailabilityBurn` ticket (+53 s), `CheckoutLatencyBurn` ticket (+68 s, for 2m45s), `CdcLagBurn` page (+113 s, for 4m45s) and ticket, `AlertmanagerFailedToSendAlerts` (+13 min) | `alerts.tsv` |

The heartbeat age moves in steps of the Iceberg sink's 60 s commit, so ages up to ~60 s are normal. Neither run had a
stale heartbeat. H-B's one bad CDC minute was a failed probe, not CDC lag.

## Game day plan (written before the runs)

| | |
|---|---|
| Hypothesis, both | CNPG promotes the replica within ~30 s. Checkouts fail only until orders reconnects through `shop-db-rw`. Replication is asynchronous, so an order acked just before the kill can be lost: every acked order id is looked up on the new primary. |
| H-A | `debezium_shop` does not exist on the new primary. Debezium (`snapshot.mode: when_needed`) finds its offset unavailable and snapshots every table again into the same epoch. An order changed between the promotion and the new slot stays stale in silver (streamed `c`/`u` rank above `r`), and the KPI misses orders created then (it counts `c`). Nothing alerts. |
| H-B | The replica holds `debezium_shop` with `failover = t, synced = t`. Debezium resumes from its offset on the promoted standby: a lag spike, no gap, no re-snapshot. Logical decoding waits for the standby listed in `synchronized_standby_slots`, so a CDC stall is measured against the time the killed pod rejoins. |
| Experiment | `chaos/gd2-kill-postgres-primary.yaml`: PodChaos pod-kill, grace 0, on the pod labelled `cnpg.io/instanceRole=primary`, after 20 min of steady load. |
| Checks before the kill (abort if any fails) | One primary and one replica. Primary `debezium_shop` with `failover = t, active = t` (#268). H-B: replica `debezium_shop` with `failover = t, synced = t`, `sync_replication_slots = on`, a dbname in `primary_conninfo`. H-A: no `debezium_shop` on the replica, `sync_replication_slots = off`. A failed query aborts; it never counts as "no slot". |
| Stop conditions | No new primary within 180 s: stop the load, collect evidence, `make down`. Host guard before each phase and once a minute. The kill is one-shot, so there is no experiment to delete. |
| Measurement | Cluster and connector status every 5 s; the k6 ack log; slots on both instances; `scripts/sre-cdc-gap.sh` over kill ±15 min. Every acked id is checked in Postgres, and acked orders are checked in bronze. Lakehouse restarts in the window make the CDC result "confounded". |

## Summary

Killing the primary cost both runs the same: no checkout succeeded for about 24 s, 211–225 of ~25,190 failed, and no
acknowledged order was lost. CNPG promoted the replica 20.6–20.7 s after the kill.

CDC is where the arms differ:
- **H-A:** Debezium found its offset gone on the new primary. It recreated the slot and snapshotted every table into
  the same epoch. For 4.07 s the new primary was up with no slot, and any write in that window would have been missed
  silently. None landed, so the gap was empty by timing.
- **H-B:** the new primary already held the synced copy of the slot. Debezium resumed on it with no snapshot. The app
  wrote 54 acknowledged orders before Debezium was back, which is exactly the race H-A is exposed to, and all of them
  streamed.

Slot sync closes the gap by design rather than by luck.

H-B also paged on CDC lag while CDC was on time. The CDC probe failed for one minute during the failover, most likely
because the Iceberg catalog it reads through keeps its database on shop-db. On a cluster only minutes past the SLI's
grace, that one minute was enough to page. The page then failed to deliver.

## H-A: where the gap could have been, and why it stayed empty

Debezium handled the failover inside its task. It never failed from Connect's point of view, so the KafkaConnector
status read RUNNING throughout and `CdcConnectorFailed` (Connect's task metric, `for: 10m`) had nothing to see.

| Time (UTC+7) | Event | Source |
|---|---|---|
| 08:37:22 | PodChaos kills shop-db-1 | `podchaos.json` |
| 08:37:24.578 | Last transaction the standby replayed; the last ack before the silence is 08:37:24.581 | `pg-shop-db-2.log`, acks |
| 08:37:26 | Debezium: producer failure on the dead connection; "Going to restart connector after 10 sec." | `connect.log` |
| 08:37:36 | Second attempt fails: no primary yet | `connect.log` |
| 08:37:41.882 | shop-db-2: promote request, "redo done at 0/1F0D78F0" | `pg-shop-db-2.log` |
| 08:37:42.827 | shop-db-2 accepts connections as primary (timeline 2) | `pg-shop-db-2.log` |
| 08:37:46 | Third attempt: "Last recorded offset is no longer available on the server … when_needed … snapshot", `CREATE_REPLICATION_SLOT "debezium_shop" LOGICAL pgoutput ( FAILOVER )` | `connect.log` |
| 08:37:46.895 | Slot consistent point 0/1F0DA950, snapshot exported with 0 transaction ids | `pg-shop-db-2.log` |
| 08:37:47 – 48 | Snapshot completed, streaming from the new slot | `connect.log` |
| 08:37:49.48 | First acked checkout after the silence | acks |
| 08:37:58 | shop-db-1 back as a streaming standby | runner |

The hazard window runs from the new primary accepting connections to the new slot's consistent point: **4.07 s**
(08:37:42.827 → 08:37:46.895).

What shows that it was empty:
- **Orders:** no `r` without `c` and no status difference over kill ±15 min. Every order is in bronze.
- **Checkout:** no ack between 08:37:24.58 and ~08:37:49.5, so no checkout committed in the window.
- **Nothing lost:** the standby replayed up to the last ack, and all 24,965 acked orders are in Postgres.
- **Not checked: other writers** (fulfillment-worker shipments, the pending-order sweeper, payments). `log_connections`
  is off, and the cluster is gone. Only 12,384 bytes of WAL lie between the end of redo and the slot's consistent
  point, where promotion wrote its end-of-recovery record and two checkpoints. That fits "no application writes" but
  does not prove it.

The window's length is set by Debezium's retry delay, 10 s (`retriable.restart.connector.wait.ms`): it reconnects
0–10 s after the promotion, depending on the phase. The app reconnected about 6.7 s after the promotion here, 2.6 s
after the slot existed. When the app wins that race, its writes before the new slot exist only as snapshot `r` rows.
Orders created then are missing from the KPI, and orders updated then stay stale in silver, silently.

## H-B: the same race, with the slot already there

| Time (UTC+7) | Event | Source |
|---|---|---|
| 12:24:16.648 | Last slot sync before the kill: the replica's `debezium_shop` equals the primary's (confirmed_flush 0/1D1E16A0) | `slots.txt` |
| 12:24:17.066 | PodChaos kills shop-db-1 | `podchaos.json`, runner |
| 12:24:19.354 | Last ack before the silence; the standby's last replayed transaction is 12:24:19.393 | acks, `pg-shop-db-2.log` |
| 12:24:26 | Debezium: producer failure; "Going to restart connector after 10 sec." | `connect.log` |
| 12:24:36 | Second attempt fails: no primary yet | `connect.log` |
| 12:24:36.443 | shop-db-2: promote request, "redo done at 0/1E130700"; the slot sync worker stops | `pg-shop-db-2.log` |
| 12:24:37.668 | shop-db-2 accepts connections as primary (timeline 2) | `pg-shop-db-2.log` |
| 12:24:37.888 | `synchronized_standby_slots` changed to `_cnpg_shop_db_1` | `pg-shop-db-2.log` |
| 12:24:42.621 | First acked checkout after the silence, written on the new primary | acks |
| 12:24:46 | Third attempt: "Obtained valid replication slot … latestFlushedLsn=0/1D1E16A0"; "A previous offset indicating a completed snapshot has been found", snapshot SKIPPED; streaming | `connect.log` |
| 12:24:46.937 | Decoding waits: "replication slot `_cnpg_shop_db_1` specified in parameter `synchronized_standby_slots` does not have active_pid" | `pg-shop-db-2.log` |
| 12:24:47 | shop-db-1 back as a streaming standby; decoding goes on with no further wait logged | runner, `pg-shop-db-2.log` |

From the promotion to Debezium streaming took 9.3 s. In that time the app wrote 54 acknowledged orders on the new
primary: the case that H-A can only show as snapshot rows. The slot's confirmed position (0/1D1E16A0) lies before the
new primary's end of redo (0/1E130700), so every one of those writes was decoded. All are in bronze as streamed rows,
and no `r` row was ingested.

The predicted stall was real and short. Decoding waited for the standby named in `synchronized_standby_slots`, and the
killed pod rejoined within a second. Not tested: a standby that does not come back. Then decoding would wait for it.

## The CDC page that was not about CDC

`CdcLagBurn` paged from 12:26:10 to 12:30:55, and its ticket fired until the end of the run. CDC was on time: the
bronze heartbeat was 8.5 s old before the kill and 11.0 s old after it. Every acked order reached bronze.

The SLI (`cdc:bronze_heartbeat_stale:minute`) counts a minute as bad when the heartbeat is 300 s or older, or when the
freshness probe fails. The bad minute at 12:26:10 has no heartbeat age sample. The exporter removes that sample when a
probe fails, so this was a failed probe, not a stale heartbeat.

A single bad minute paged only because the cluster was young:
- **Why it paged:** the SLI's first sample came at 12:15:10, after the exporter's 15-minute grace, so at 12:26:10 the
  failed minute was 1 of 12. That is 8.3% over every window, above the slow page condition (6× burn, 6% at a 99%
  objective, over both 30 m and 6 h).
- **Why it cleared:** the page stopped at 12:30:55, when the ratio fell to 1 of 17 (5.9%). The ticket (3% over 2 h and
  1 d) still held at the end of the run (1 of 32).
- **On an older cluster:** with a full hour of history, the same minute is 1.7% over 1 h and 0.3% over 6 h, under every
  page threshold. It would only spend a little error budget per failover.

**Inferred: the probe failed during the shop-db failover.** The probe reads `bronze.heartbeat` through Trino's
`lake_ro` catalog, which goes through Polaris. Polaris keeps its catalog database on shop-db
(`jdbc:postgresql://shop-db-rw…/catalog`, `deploy/platform/iceberg-catalog/base/secret-copy.yaml`). So the CDC
measurement goes down with the database it measures. The exporter logs were not captured and the cluster is gone, so
the cause is inferred, not proven.

H-A escaped by timing. Its probes are a minute apart, and the one in the failover minute succeeded, so it ran before or
after the outage (08:37:22 → 08:37:43). An outage of ~20 s in a 60 s probe cycle hits a probe roughly one failover in
three.

The page then went nowhere. Pages and tickets route to Discord, and the local webhook is a placeholder (`.invalid`).
H-B's page and tickets failed often enough that `AlertmanagerFailedToSendAlerts` and
`AlertmanagerClusterFailedToSendAlerts` (critical) fired from 12:37:10. H-A's single ticket did not trip them.

## Result vs hypothesis

| Prediction | Measured | |
|---|---|---|
| Promotion within ~30 s | +20.7 s (H-A), +20.6 s (H-B) | as predicted |
| Writes fail only for seconds | no ack for 23–25 s; 225 / 211 checkouts failed | as predicted |
| Acked orders can be lost (async) | 0 lost in both: the standby had replayed up to the last ack | none lost at this load |
| H-A: offset unavailable, snapshot into the same epoch | exactly that, slot recreated with `FAILOVER` | as predicted |
| H-A: stale silver and KPI misses, no alert | 0 and 0: no write landed in the 4.07 s window; no alert | mechanism confirmed, gap empty at this timing |
| H-B: Debezium resumes from its offset on the synced slot, no snapshot | resumed at the pre-kill confirmed position, snapshot skipped, 0 `r` rows; 54 orders written before it resumed all streamed | as predicted |
| H-B: decoding waits for the standby | logged at 12:24:46.937; the standby rejoined within a second | as predicted, short at this timing |
| No CDC alert (both) | H-B paged `CdcLagBurn` on a failed probe | not as predicted: the measurement shares fate with shop-db |

## What went well

- Every pre-kill check passed and proved its arm on the live cluster, including #268's failover flag on the primary's
  slot. Neither arm was assumed from the chart.
- CNPG promoted in about 20.6 s and brought the old primary back as a standby within 30–36 s.
- No acked order was lost, and none was missing from bronze.
- Slot sync worked end to end: the synced copy, the resumed offset, and the switch of `synchronized_standby_slots` to
  the rejoined standby.
- The host guard did its job twice. It caught H-B's first-run sleep at the first wake, and it held the re-run to an
  open lid and enough battery.
- The Chaos Mesh hard refresh stayed Synced/Healthy with no new rollout in every run: the live check for #259.

## What went wrong

- **H-B needed a second run.** The lid was closed on battery 8m46s after the first run's kill. Its Connect log and gap
  check were lost.
- **The first launch was refused.** `make up` needs a 40-hex SHA or a branch/tag name, and the launch used a short
  SHA. Nothing ran; the relaunch used the full SHA.
- **A false page.** The CDC SLI read a failed probe as lag because its read path depends on shop-db. Because the cluster
  was young, one bad minute was enough to page. And the page was not delivered.
- **A failover raises no alert of its own.** The signals were the checkout burn tickets and, in H-B, the false CDC
  page.
- **H-A's safety was timing, not design.** Without slot sync, a 0–10 s window remains after every failover, and
  nothing would detect writes lost in it.
- **The re-run's smoke gate check failed 2 of 13.** The CDC SLI series was missing. `sre-gate-check.sh` stops
  excluding the exporter's first 900 s at exactly 900 s, but the recording rule only produces its first sample one or
  two evaluations later. `make up` was fast, and the gate check ran at 932 s. The series was present 39 s later.

## Action items

| Action | Owner | Status |
|---|---|---|
| Re-run H-B (same runner and revision, fresh cluster) | sf-sre | done (12:24 kill, valid) |
| H-A's hazard window exists only without slot sync. Keep sync on, and decide whether to also guard against running without it (the replica's `debezium_shop` not `synced`), shorten Debezium's `retriable.restart.connector.wait.ms`, or detect a recreated slot | sf-data, sf-sre | decide |
| False CDC page: tolerate one failed probe minute in the CDC SLI, retry the probe within the minute, or move the Polaris catalog database off shop-db. Collect the exporter logs in the next failover game day to confirm the cause. The page also depended on the SLI's short history after its grace, and that weighting applies to every young cluster | sf-data, sf-sre | decide |
| Check the other writers in the hazard window next time: payments and shipments in `sre-cdc-gap.sh`, or `log_connections` for the game day | sf-sre | open |
| Decide whether a CNPG failover should raise its own ticket (timeline change on shop-db) rather than only the checkout burn | sf-sre | decide |
| Alertmanager delivery: the local Discord webhook is a placeholder. These tickets and H-B's page reached nobody, and H-B tripped `AlertmanagerFailedToSendAlerts` (critical) | user decision | open |
| `sre-gate-check.sh`: exclude the exporter's first 900 + 120 s, so the gate check does not run before the first post-grace sample | sf-sre | open |
