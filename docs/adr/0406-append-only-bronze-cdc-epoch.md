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
  on the rt slot of 2026-10-10). Locally the epoch is the unix time in seconds; on AWS it comes from SSM via ESO.
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
- When to revisit: if the Iceberg sink stops keying its offsets by control topic.
