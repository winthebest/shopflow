# Runbook: a shop-db standby is not streaming

| | |
|---|---|
| Alert | `PostgresStandbyNotStreaming` (ticket) |
| Rule | [`deploy/platform/slo/base/shop-db-replication.prometheusrule.yaml`](../../deploy/platform/slo/base/shop-db-replication.prometheusrule.yaml) (tests: `slo/tests/shop-db-replication.test.yaml`) |
| Related | [gameday.md](gameday.md) scenario 2, [cdc-lag.md](cdc-lag.md), [wal-retained.md](wal-retained.md) |

## What it means

When shop-db runs more than one instance, each standby streams WAL from the primary through a physical
high-availability slot (`_cnpg_<instance>`) that CloudNativePG keeps on the primary. The alert fires when that slot
has had no reader for 5 minutes: the standby is down, cannot connect, or cannot follow the primary.

- **Failover is not safe.** A standby that does not stream is behind: promoting it would lose the writes it
  never received.
- **CDC waits for it** when logical slot sync is on: the primary sends Debezium only changes that the standbys listed
  in `synchronized_standby_slots` (CNPG maintains it) have received. Expect `CdcLagBurn` to follow.
- **WAL accumulates on the primary** for the slot, like an unread Debezium slot.

Right after a failover the old primary has up to 5 minutes to come back as a standby (CNPG rewinds and restarts it)
before this fires.

## Check

```sh
K="kubectl --context k3d-<cluster> -n shop"
$K get cluster shop-db -o jsonpath='{.status.phase}{"  primary="}{.status.currentPrimary}{"  ready="}{.status.readyInstances}{"/"}{.status.instances}{"\n"}'
$K get pods -l cnpg.io/cluster=shop-db -L cnpg.io/instanceRole
$K logs <standby-pod> -c postgres --since=15m | grep -i -E 'replication|rewind|timeline|could not'
```

On the primary (`$K exec -it <primary-pod> -c postgres -- psql -d shop`):
`SELECT application_name, state, sent_lsn, replay_lsn FROM pg_stat_replication;` (the standby should be `streaming`)
and `SELECT slot_name, active, restart_lsn FROM pg_replication_slots WHERE slot_type = 'physical';`.

## Fix

- **Standby pod not Running / CrashLoop**: read its log. After a failover, a failed `pg_rewind` or a missing WAL
  segment means the old primary cannot rejoin: delete its PVC and pod so CNPG clones it again (`$K delete pvc
  <pod> && $K delete pod <pod>`), only once the cluster's primary is healthy.
- **Running but not streaming**: `pg_hba`/NetworkPolicy between instances (ports 5432 and 8000 are allowed by the
  shop-db policy), or the volume is full.
- **Recovery is slow but progressing** (large WAL backlog): wait while `replay_lsn` moves; nothing else to do.

## After

Check `PostgresStandbyNotStreaming` resolved and, with slot sync, that the standby's copy of `debezium_shop` shows
`synced = t` again (`SELECT slot_name, synced, failover FROM pg_replication_slots;` on the standby).
