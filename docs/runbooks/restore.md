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

## When a recovery fails

| Symptom | Cause | Action |
|---|---|---|
| New Cluster stuck in `Setting up primary`, logs "no target backup found" | The chain has no completed base backup, or the PITR mark is before it | Pick a later mark, or recover latest. The base backup must be older than the mark |
| "Expected empty archive" | `pg.serverName` names a chain that already has WAL | Use a new chain name (the drill stamps one per run) |
| Local: Secret `shop/shop-db-backup-s3` missing, archiving fails | The plugin app's copy Job has not run | Check the `cnpg-barman-plugin` app and the SeaweedFS identity Secret in `lakehouse` |
| Argo CD apps frozen after a drill | The controller is still at 0 replicas | `kubectl -n argocd scale statefulset argocd-application-controller --replicas=1` |
