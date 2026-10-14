# 0406. Append-only bronze with a CDC epoch, and one Iceberg control topic per epoch

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Red-team findings 1 and 2. Postgres "lives" change: every `make up` / `cloud-up`, every restore or PITR, every
forced re-snapshot. Across lives, LSNs are not comparable (a restored timeline reuses lower LSNs), primary keys can
be reused, and a snapshot never emits deletes for rows that disappeared. Separately, the Iceberg sink stores
control-topic offsets in each table snapshot (summary key `kafka.connect.offsets.<control topic>.<connect group>`,
`Coordinator.java`) and drops written data whose control-topic offset is lower than the stored one: after a new
Kafka the offsets restart at 0, so with the same control topic every commit would be silently dropped.

## Decision

- Bronze is append-only: `ExtractNewRecordState` with `_op`, `_lsn`, `_source_ts_ms`; deletes are rewritten to a
  row with `_op='d'` (no tombstones); snapshot rows have `_op='r'`.
- Every event carries `_cdc_epoch` (Debezium `InsertField`, value read at task start from Secret
  `kafka/cdc-epoch` through the config provider). A new life = a new epoch, recorded in `meta.cdc_epochs`;
  silver reads only the newest epoch whose snapshot completed, ordered by `_lsn` inside that epoch.
- The sink's control topic is `iceberg-control-<epoch>`, created by the epoch tooling with the epoch.
- Epoch tooling: `scripts/cdc-epoch.sh new` (before the connectors sync: Secret `kafka/cdc-epoch` + KafkaTopic
  `iceberg-control-<epoch>`; needs only the Strimzi CRDs) and `scripts/cdc-epoch.sh wait` (inserts the epoch into
  `meta.cdc_epochs` and sets `snapshot_completed_at` once Debezium's `SnapshotCompleted` metric is 1 and bronze holds
  snapshot rows of the epoch). `make up` calls `ensure`, which does what `new` does only on a fresh cluster and keeps
  the epoch of a running one: running connectors stamp the epoch they read at task start, so a rotation without
  restarting them leaves bronze on the old epoch, and Debezium's metric still reports the old task's snapshot (seen
  on the rt slot of 2026-10-10). After the apps are healthy, `make up` also runs `wait`, because silver reads only
  an epoch whose snapshot is recorded and nothing else recorded it on a fresh cluster (#189). `wait` returns at once
  for an epoch already complete, so a rerun after a Connect restart does not hang. Locally the epoch is the unix time
  in seconds; on AWS it comes from SSM via ESO.
- The Connect group stays `connect-<connector name>`: the coordinator is elected from that group's members
  (`CommitterImpl.hasLeaderPartition`), and `iceberg.connect.group-id` is documented as "should not be set under
  normal conditions". A per-epoch control topic already gives a fresh snapshot-summary key.
- `iceberg.control.commit.interval-ms=60000` and `iceberg.control.commit.max-consecutive-failures=10`.

## Alternatives considered

| Option | Why not |
|---|---|
| Upsert/merge in the sink keyed by primary key | Wrong after restore/PITR (older LSN wins nothing, reused keys collide); a re-snapshot cannot delete vanished rows |
| Dedup on `max(_lsn)` across all history | LSNs restart with a new timeline |
| `iceberg.connect.group-id=iceberg-<epoch>` (as first planned) | Without also overriding the consumer group the coordinator is never elected; with both, a re-snapshot in the same Kafka re-reads old epochs from offset 0 |
| Keep the default control topic and reset table summaries | Manual surgery on every table after every `make up` |

## Consequences

- Positive: bronze is a full history; silver is rebuilt correctly after any restore or re-snapshot.
- Observed in the smoke test: under the default `REPLICA IDENTITY`, a delete row carries only a reliable primary
  key; Debezium fills the other NOT NULL columns with empty/zero values (`status=''`, `total=0.00`). Consumers
  must use `_op='d'` rows for the key only.
- Negative / risks: bronze grows with every epoch (retention handled in Phase 12); the epoch tooling must create
  the control topic before the sink starts (the connector auto-restarts until it exists).
- Verified on a cluster:
  - 2026-10-10, `k3d-sf-app`: a re-snapshot replayed 21,339 orders as `r` under a new epoch with 0 duplicate
    shipments (docs/runbooks/cdc-resnapshot.md), and a rerun of `make up` kept the epoch (#170).
  - 2026-10-12, `k3d-sf-sre` (B2 slot): `make up` was "ready in 405s (apps 337s, CDC snapshot 68s)", and
    `meta.cdc_epochs` held the new epoch with `snapshot_completed_at` when it returned (#189).
  - 2026-10-12, `k3d-sf-data` (rt slot 2): a second `make up` on the running cluster logged "snapshot already
    completed at 2026-10-12 05:48:23" and was ready in 19 s. The epoch Secret, `meta.cdc_epochs`, the control topics
    and the connector generations were identical before and after (#189).
  - 2026-10-14, `k3d-sf-data` (P5 slot, `9fa67e1`): a re-snapshot following docs/runbooks/cdc-resnapshot.md, with
    5 paid orders (ids 1-5) deleted in Postgres while the source connector was stopped (23:18:24-29Z). k6 kept
    sending 2 checkouts/s.
    - The new epoch 1791933505 finished its snapshot in 70 s (23:18:31-23:19:41Z).
    - The old epoch 1791925589 holds ids 1-5: 10 order rows, 9 order item rows and 5 payment rows. The new epoch
      holds none of them. After the next `dbt_build`, neither do silver `orders`, silver `payments` or gold
      `fct_orders`.
    - The reconciliation DAG passed 5 of 5 two-way checks against Postgres (customers, products, orders, order_items,
      payments).
    - Silver equals Postgres: 26,409 orders, 52,829 order items and 26,409 payments. k6 made 26,414 checkouts =
      26,409 orders + the 5 deleted.
    - During the re-snapshot, only the bronze freshness probes failed (23:19:29-23:19:59Z). The CDC SLI counts a
      failed probe as a stale minute, and the `CdcLagBurn` ticket fired from 23:21:29Z until about 00:45Z.
- When to revisit: if the Iceberg sink stops keying its offsets by control topic.
