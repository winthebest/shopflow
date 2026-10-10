# Restore shop-db from its backup chain

Owner: sf-cloud. shop-db is a CNPG cluster; its WAL archive and base backups go through the barman-cloud plugin
to an object store: SeaweedFS bucket `pg-backup` locally (profile `drill`, or `data`), S3 on AWS. A recovery
creates a **new** Cluster from a chain (`pg.recoveryFrom`, latest or a PITR mark `pg.recoveryTargetTime`), and the
new Cluster archives to a **new** chain (`pg.serverName`): a chain is never written by two clusters.

- AWS: cloud-up does this every session (pointer in SSM, the chain is checked before anything bills); see
  [`cloud-session.md`](cloud-session.md) and ADR 0511.
- Local: `scripts/restore-drill.sh`, below. The Phase 7 restore drill measures RPO and RTO with it.

## Local drill (k3d, profile `drill`)

The drill destroys the database while k6 writes, without a final backup, and recovers it from its chain. It needs
about 4 GB in the Docker VM (`core` 3.3 GB + SeaweedFS + the plugin), so it can run beside `sf-main`. `drill` and
`data` are mutually exclusive (both own the SeaweedFS app).

```sh
make up CLUSTER=<slot> PROFILES=core,drill          # make up starts the chain (pg.serverName=shop-db)
scripts/restore-drill.sh prepare --cluster <slot>    # base backup on that chain -> out/drill/pointer.json
scripts/restore-drill.sh run --cluster <slot>        # latest
scripts/restore-drill.sh run --cluster <slot> --pitr 30   # PITR: 30 s before the disaster
```

`--profiles` and `--revision` default to what the cluster's root apps use (the `root-*` apps and root-core's
revision), so re-applying the root apps for the recovery never moves the other apps to `main`.

Checks for the first drill slot, before `prepare`. Record the growth rate under Results.

- [ ] Job `cnpg-system/shop-db-backup-copy` Succeeded, so Secret `shop/shop-db-backup-s3` exists.
- [ ] `kubectl -n shop get clusters.postgresql.cnpg.io shop-db -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")].status}'`
      prints `True`.
- [ ] Bucket `pg-backup` exists and receives WAL:
      `kubectl -n lakehouse exec deploy/seaweedfs -- sh -c 'echo "fs.du /buckets/pg-backup" | weed shell'`.
- [ ] Growth rate: run `fs.du` again one hour later, and write down **which profiles** were running.
      `archive_timeout: 60s` only switches segment when WAL was written since the last switch:
      - with `data`, the Debezium heartbeat writes every 10 s, so a segment goes out every minute;
      - with `drill` alone, an idle shop may archive almost nothing, and only load (k6) makes it grow.
      gzip shrinks the mostly empty segments. The rate decides how long a drill cluster can stay up.
- [ ] Archive alerts (sf-sre, [wal-archive.md](wal-archive.md); needs `obs-lite`, so this slot runs
      `PROFILES=core,obs-lite,drill`):
      - healthy: `shopflow:pg_wal_archive_pending:segments` stays at 0–2;
      - blocked: pause the Argo CD controller as in `run` (scaling SeaweedFS alone is undone by selfHeal), scale
        `lakehouse/seaweedfs` to 0, and keep k6 writing every minute. Without WAL activity no segment is ready,
        the archiver never fails, and the alert has nothing to see. The first archive attempt fails within about
        a minute. Two outcomes are by design; record which one happened:
        - the archiver fails fast (connection refused, no endpoint): `shopflow:pg_wal_archive_failing:bool` = 1
          from about +1 min, so `ShopDbWalArchiveFailing` fires around +11–14 min;
        - the archiver hangs (plugin retries, no error): `failing` stays 0, the pending segments pass 10 around
          +11 min, so the alert fires around +21–24 min.
      - Keep one log line of the sidecar at the failure, and confirm the container name (`plugin-barman-cloud`).
      - Restore SeaweedFS and the controller **right after** the test (the pause stops reconciliation for the
        whole cluster). Record the pause and resume times under Results.
      - Check that pending returns to 0–2 and the alert resolves, and record how long the backlog took to drain.
      - Evidence: `make sre-gameday-evidence SRE_GATE_CONTEXT=k3d-<slot> FROM=<block time, RFC 3339>` gives
        alerts.tsv and the archive series.

What `run` does:

1. **Preflight:**
   - profile `drill` is Synced/Healthy;
   - shop-db is Ready and still archives to the chain in `out/drill/pointer.json`;
   - that chain's base backup is completed.
2. **k6** writes at `--rate` checkouts/s (default 20) for `--warmup` seconds (default 120) through the cluster's
   HTTPS port. Every acked order is logged to `out/drill/run-<stamp>/acks.jsonl`.
3. **Disaster:**
   - the Argo CD application controller is scaled to 0, otherwise selfHeal would recreate the Cluster with initdb
     and the old chain name;
   - the shop-db pod is force-deleted (`--grace-period=0`, so there is no final WAL archive), then its Cluster and
     PVC;
   - k6 stops.
4. **Recovery:**
   - `platform-root-apps.sh --overlay local` gets `pg.serverName=<new chain>`, `pg.recoveryFrom=<old chain>` and,
     for PITR, `pg.recoveryTargetTime`;
   - the controller is scaled back to 1, and Argo CD creates the new Cluster.
   - If the script stops while the controller is paused, its exit trap scales the controller back.
5. **Measure:**
   - the surviving order ids are read before anything writes again;
   - one checkout must succeed;
   - the result goes to `result.json`.
6. **Next chain:** a base backup on the new chain, and the pointer moves to it, so the next drill starts there.

| Field in `result.json` | Meaning |
|---|---|
| `acked`, `lost` | Orders acked before the disaster; the acked ones that are not in `orders` after the recovery |
| `rpo_seconds` | Last ack minus the last ack whose order survived. Both timestamps come from k6's clock, so no clock is compared with another. `0` when nothing acked was lost |
| `rto_db_seconds`, `rto_service_seconds` | Disaster → Cluster Ready; disaster → a checkout succeeds |
| `pitr_before_mark_lost`, `pitr_after_mark_kept` | PITR only: acks more than 2 s before the mark that are missing, and acks more than 2 s after it that survived. Both must be 0 |

Expectations:

- With `archive_timeout: 60s`, RPO stays under about a minute. Losing the WAL segment in progress is the point of
  an abrupt disaster.
- `lost` is about RPO × rate.
- After a drill the cluster archives to the drill's chain. Run `make down`, or `make up` with the drill's
  `PG_SERVER_NAME` (a plain `make up` points shop-db back at the default chain `shop-db`).

## Results

| Date | Where | Mode | acked | lost | RPO | RTO db / service | Notes |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

`pg-backup` growth (profiles; idle / under k6): _to measure in the first slot_.
RAM of `core,obs-lite,drill` (`docker stats` of the k3d nodes): _to measure in the first slot_.
Archive alert check: block / resume times, outcome (fast fail or hang), alert fired at, resolved at, backlog
drained in: _to measure in the first slot_.

## When a recovery fails

| Symptom | Cause | Action |
|---|---|---|
| New Cluster stuck in `Setting up primary`, logs "no target backup found" | The chain has no completed base backup, or the PITR mark is before it | Pick a later mark, or recover latest. The base backup must be older than the mark |
| "Expected empty archive" | `pg.serverName` names a chain that already has WAL | Use a new chain name (the drill stamps one per run) |
| Local: Secret `shop/shop-db-backup-s3` missing, archiving fails | The plugin app's copy Job has not run | Check the `cnpg-barman-plugin` app and the SeaweedFS identity Secret in `lakehouse` |
| Argo CD apps frozen after a drill | The controller is still at 0 replicas | `kubectl -n argocd scale statefulset argocd-application-controller --replicas=1` |
