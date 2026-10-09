# 0413. Silver = current state of the latest completed CDC epoch, rebuilt in full

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Bronze keeps every CDC row of every epoch (docs/adr/0406). `_lsn` orders changes only within one Postgres/Kafka life,
and a key that disappears while CDC is down leaves no delete event behind: only a new epoch's snapshot shows it is
gone.

## Decision

Each silver table (`data/dbt/macros/cdc.sql`):

1. Reads only bronze rows whose `_cdc_epoch` is the newest epoch in `pg.meta.cdc_epochs` with
   `snapshot_completed_at` set (written by `scripts/cdc-epoch.sh wait`).
2. Ranks rows per key by `(_op <> 'r') desc, _lsn desc` (streaming changes beat snapshot reads), keeps rank 1, and
   drops the key if that row is a delete. A key absent from the epoch is deleted.
3. Is materialized as a `table`, rebuilt in full on every run, with `on_table_exists: replace`
   (`CREATE OR REPLACE TABLE`). Gold does the same.

When no epoch has completed, the test `assert_completed_cdc_epoch_exists` fails the run, so an empty silver never
passes as green.

Verified offline on 2026-10-09 against the Kafka Connect smoke stack (real Debezium → Iceberg sink → Polaris +
SeaweedFS) with Trino 483: snapshot rows, updates after the snapshot, a deleted snapshot row and an order deleted
with its children all came out right. Reconciliation at lag 0 passed for all five tables and failed on a row changed
in Postgres but not yet in silver. All 10 tables and 37 tests took 9 seconds.
On the Polaris REST catalog, `replace` keeps one table: a second run added a second snapshot to
`silver."orders$snapshots"`. Each snapshot has no parent, so time travel works by snapshot id or timestamp, but there
is no incremental lineage. Glue (AWS, docs/adr/0506) remains to be checked in Phase 6.

## Alternatives considered

| Option | Why not |
|---|---|
| Incremental merge on `_lsn` | `_lsn` restarts with a new epoch; a key deleted while CDC was down would survive forever |
| Incremental on `_ingested_at` + lookback within the epoch | Only an optimization; kept for when a full rebuild exceeds 5 minutes |
| `on_table_exists: rename` (adapter default) | Creates a new Iceberg table each run and loses its snapshot history |
| Mixing epochs (latest row across all epochs) | Ranking by `_lsn` across epochs is meaningless |

## Consequences

- Positive: a re-snapshot (new epoch) corrects silver without any manual cleanup, including deletes that happened
  while CDC was down.
- Negative / risks: each run rewrites every silver/gold table (fine at demo size; Phase 11/12 retention and
  compaction see one snapshot per run without parents).
- When to revisit: `dbt build` takes more than 5 minutes, or Glue rejects `CREATE OR REPLACE` (then `drop`, and silver
  and gold are declared "rebuilt from bronze, retention ≈ 0").
