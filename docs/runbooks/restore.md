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
about 4 GB in the Docker VM (`core` 3.3 GB + SeaweedFS + the plugin); with `obs-lite` for the archive alerts,
6.1 GiB was measured. `drill` and `data` are mutually exclusive (both own the SeaweedFS app).

```sh
make up CLUSTER=<slot> PROFILES=core,drill          # make up starts the chain (pg.serverName=shop-db)
export KUBECONFIG="$(k3d kubeconfig write <slot>)"   # make up does not touch the default kubeconfig
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
   - the shop-db Application is held (`argocd.argoproj.io/skip-reconcile=true`);
   - the disaster mark is taken, then the shop-db pod is force-deleted (`--grace-period=0`, so there is no final
     WAL archive), then its Cluster and PVC;
   - k6 stops. It must not run through the recovery: the recovered sequences hand out the ids of lost orders
     again, and a new order with a lost id would count as "survived".
4. **Recovery:**
   - `platform-root-apps.sh --overlay local` gets `pg.serverName=<new chain>`, `pg.recoveryFrom=<old chain>` and,
     for PITR, `pg.recoveryTargetTime`;
   - the controller is scaled back to 1. The shop-db Application stays held until root-core has rendered those
     params into it, then it is released and Argo CD creates the new Cluster. Without the hold, the restarted
     controller syncs shop-db with its old values first and CNPG bootstraps an empty database (bootstrap runs only
     at creation; a recovery stanza added a few seconds later never runs).
   - The Cluster must have been created "from backup" (CNPG's `CreatingInstance` event after the disaster), or the
     drill stops. The bootstrap Job is no evidence: CNPG deletes it once it succeeds.
   - If the script stops while the controller is paused or the Application held, its exit trap restores both.
5. **Measure:**
   - the surviving order ids are read before anything writes again;
   - the `orders` and `payments` id sequences move past the highest acked order id plus 1000, so no lost
     customer's order id is handed to a new order;
   - one checkout must succeed;
   - the result goes to `result.json`.
6. **Next chain:** a base backup on the new chain, and the pointer moves to it, so the next drill starts there.

| Field in `result.json` | Meaning |
|---|---|
| `acked`, `lost` | Every ack in the log (k6 stops right after the kill); the acked ones that are not in `orders` after the recovery |
| `rpo_seconds` | Last ack minus the last ack whose order survived. Both timestamps come from k6's clock, so no clock is compared with another. `0` when nothing acked was lost |
| `rto_db_seconds`, `rto_service_seconds` | Disaster → Cluster Ready; disaster → a checkout succeeds |
| `pitr_before_mark_lost`, `pitr_after_mark_kept` | PITR only: acks more than 2 s before the mark that are missing, and acks more than 2 s after it that survived. Both must be 0 |

Expectations:

- RPO is bounded by `archive_timeout: 60s`: the segment in progress dies with the PVC, so RPO falls anywhere in
  0–60 s depending on where in the minute the disaster lands. Three measurements are a spread, not an average to
  quote. Lowering it is a trade-off, not a fix: a shorter `archive_timeout` ships more nearly empty segments
  (storage), and a synchronous streaming replica (two CNPG instances; the drill runs one) costs a second instance.
- `lost` is about RPO × rate. The lost acked orders (from the ack log, which lives outside the cluster) are the
  list to reconcile with the payment provider after a disaster: those customers may have been charged.
- After a drill the cluster archives to the drill's chain. Run `make down`, or `make up` with the drill's
  `PG_SERVER_NAME` (a plain `make up` points shop-db back at the default chain `shop-db`).

## Results

2026-10-12, k3d `sf-cloud`, `core,obs-lite,drill` at 65dca7e, 20 checkouts/s, 120 s of load before each disaster.
RPO uses every ack (official); "lost" are acked orders missing after the recovery.

| Run | Mode | acked | lost | RPO | RTO db / service | Notes |
|---|---|---|---|---|---|---|
| 1 | latest | 2725 | 1120 | 56.0 s | not counted | **Failed run**: Argo CD recreated the Cluster with initdb before the recovery params (race), and the backup pointer had a null chain. Recovered by hand to measure RPO. Also found: the disaster mark was taken 16 s early, which made RPO look better (38.8 s) |
| 2 | latest | 2745 | 563 | 28.1 s | 68 s / not measured | Recovery correct; the drill stopped on a false "initdb" (it checked the bootstrap Job, which CNPG deletes) |
| 3 | latest | 2757 | 927 | 46.4 s | 85 s / 86 s | Valid. The checkout after the recovery got id 11352, an id already acked to a customer and lost (11334–12260): the id-reuse hazard, hence the sequence bump |
| 4 | latest + sequence bump | 2727 | 882 | 44.1 s | 83 s / 84 s | Valid. The checkout after the recovery got id 16988, past every acked id (13261–15987) |
| 5 | PITR, mark 30 s before the disaster (04:46:13Z) | 2686 | 951 | (by design) | 68 s / 68 s | Valid. 0 acks more than 2 s before the mark missing, 0 acks more than 2 s after it kept. The lost orders are the ones after the mark |

The RPOs of runs 1–4 (56, 28, 46, 44 s) all fall in the 0–60 s window of `archive_timeout`. In every run all acks
up to the last archived segment survived (Postgres reconciliation by order id).

- `make up` took 500 s (Gate 2: ~300 s), most of it in the chain cert-manager → cnpg → `cnpg-barman-plugin` →
  `shop-db`.
- `pg-backup` growth with `core,obs-lite,drill`: 2.35 MB idle after `make up`, then about **21 MB/h at 5
  checkouts/s** (about 0.5 MB per gzip'd segment, one segment a minute while there are writes); WAL is written at
  about 53 KB/s at 5 checkouts/s.
- RAM of `core,obs-lite,drill`: **6.11 GiB** (server 3.94 + agent 2.13 + load balancer and tools).
- Archive alert check (with sf-sre): blocked 04:01:48Z with k6 at 5/s. **Fast-fail** outcome: each archive attempt
  failed after about 6.5 s ("Error invoking barman-cloud-wal-archive"), so `failing` was 1 from about +2 min.
  `ShopDbWalArchiveFailing` fired at 04:13:03Z (+11 min 15 s) with 11 segments pending. Resumed 04:13:36Z
  (controller back to 1); the backlog drained and the alert resolved before 04:14:16Z (under 40 s).
  `BacklogCritical` did not fire. The sidecar `plugin-barman-cloud` is a native sidecar (an init container).
  Evidence: `out/gameday-20261012T041457Z/`.

Next: once the ack carries the idempotency key and the key is stored with the order, survivors can be matched by
key instead of id. k6 could then keep running through the disaster.

## When a recovery fails

| Symptom | Cause | Action |
|---|---|---|
| New Cluster stuck in `Setting up primary`, logs "no target backup found" | The chain has no completed base backup, or the PITR mark is before it | Pick a later mark, or recover latest. The base backup must be older than the mark |
| "Expected empty archive" | `pg.serverName` names a chain that already has WAL | Use a new chain name (the drill stamps one per run) |
| Local: Secret `shop/shop-db-backup-s3` missing, archiving fails | The plugin app's copy Job has not run | Check the `cnpg-barman-plugin` app and the SeaweedFS identity Secret in `lakehouse` |
| Argo CD apps frozen after a drill | The controller is still at 0 replicas | `kubectl -n argocd scale statefulset argocd-application-controller --replicas=1` |
| shop-db Application never syncs after a drill | It is still held | `kubectl -n argocd annotate applications.argoproj.io shop-db argocd.argoproj.io/skip-reconcile-` |
| Recovered Cluster is empty (no `orders`), event "Primary instance (initdb)" | The Cluster was created before the recovery params reached the shop-db Application | Recover again with the Application held (what `run` does); the chain still holds the data |
