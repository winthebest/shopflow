# Runbook: shop-db WAL archiving failing

| | |
|---|---|
| Alerts | `ShopDbWalArchiveBacklogCritical` (page), `ShopDbWalArchiveFailing` (ticket) |
| Rule | [`deploy/platform/slo/base/wal-archive.prometheusrule.yaml`](../../deploy/platform/slo/base/wal-archive.prometheusrule.yaml) (tests: `slo/tests/wal-archive.test.yaml`) |
| Related | [restore.md](restore.md) (backup chain, drill), [wal-retained.md](wal-retained.md) (WAL held by the Debezium slot: a different cap) |

## What it means

shop-db archives every WAL segment through the barman-cloud plugin (a sidecar in each shop-db pod) to object
storage: SeaweedFS bucket `pg-backup` locally, S3 on AWS. Postgres deletes a segment only after it is archived.
While archiving fails or hangs, every new segment stays in `pg_wal` on the database volume (5Gi).
`max_slot_wal_keep_size` does not help: it caps replication slots only.

With `archive_timeout: 60s` and the CDC heartbeat writing every 10 s, Postgres switches segment every minute even
on an idle shop: about one 16 MiB segment a minute, ~1 GB/h, more under load. When the volume is full, Postgres
stops and checkout fails.

- **Failing** (ticket): for 10 minutes the archiver's last failure is newer than its last success, or more than
  10 segments wait to be archived (an archive command that hangs records no failure). Fix within the working
  session.
- **Backlog critical** (page): 80 segments (1.25 GiB) wait, a quarter of the volume, about 80 minutes after the
  archive stopped at one segment a minute: roughly 3 hours remain before the database stops. Act now.

Point-in-time recovery is also blind past the last archived segment while this lasts: RPO grows with the backlog.

## Triage

1. How bad and how fast, in Grafana Explore:
   - `shopflow:pg_wal_archive_pending:segments`: segments waiting (×16 MiB);
   - `deriv(shopflow:pg_wal_archive_pending:segments[15m]) * 60`: segments a minute;
   - `cnpg_pg_stat_archiver_failed_count`, `cnpg_pg_stat_archiver_seconds_since_last_archival`.
2. Why, from the plugin sidecar (`plugin-barman-cloud`; `kubectl -n shop get pod shop-db-1 -o
   jsonpath='{.spec.containers[*].name}'` lists the containers):
   `kubectl -n shop logs shop-db-1 -c plugin-barman-cloud --since=30m | tail -50`, and Postgres' own view:
   `kubectl -n shop exec shop-db-1 -c postgres -- psql -c 'select * from pg_stat_archiver'`.
3. Typical messages:
   - `AccessDenied` / `InvalidAccessKeyId` / `401`: credentials. Locally the Secret `shop/shop-db-backup-s3` (copied
     by the `cnpg-barman-plugin` app from SeaweedFS' identity in `lakehouse`); on AWS the pod's IAM role.
   - connection refused / timeout to port 8333: SeaweedFS down (`kubectl -n lakehouse get pods`), or the
     NetworkPolicy `shop/shop-db-backup` (network-policies app; local overlay → SeaweedFS 8333, aws overlay → S3)
     missing or out of sync.
   - `NoSuchBucket`: bucket `pg-backup` missing.
   - "Expected empty archive" / "WAL already exists": the chain `pg.serverName` already holds WAL from another
     cluster ([restore.md](restore.md)).
4. Disk left: `kubectl -n shop exec shop-db-1 -c postgres -- df -h /var/lib/postgresql/data`.

## Mitigation

| Situation | Action |
|---|---|
| Credentials, bucket or SeaweedFS | Fix the cause; the archiver retries by itself and works through the backlog in order. Watch `shopflow:pg_wal_archive_pending:segments` fall |
| NetworkPolicy missing | Sync the `network-policies` app; do not open egress by hand |
| Cannot fix before the volume fills | Grow the volume (`storage.size` in the shop-db chart, if the storage class allows expansion) to buy hours. Do not delete files in `pg_wal` |
| Archive will not come back (backups not needed on this session) | Remove the plugin from the Cluster in git so the archive command stops; Postgres recycles the WAL. The backup chain is broken from then on: take a new base backup when archiving is back, and record it |

## After

Check that `ShopDbWalArchiveFailing` resolved and pending segments are back to 0–2, that a base backup after the
incident exists (`kubectl -n shop get backups`), and write down the gap in the archive (first and last unarchived
segment) in the postmortem if a page fired.
