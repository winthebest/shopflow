# Runbook: re-snapshot CDC into a new epoch

| | |
|---|---|
| When | Changes were lost between Postgres and bronze (slot or Kafka data lost, reconciliation finds missing rows), or a replay is wanted on purpose (idempotency test of a consumer) |
| Effect | Debezium reads every shop table again as `_op = 'r'` under a new CDC epoch; silver switches to that epoch once its snapshot is recorded (docs/adr/0406). Bronze keeps the old epoch as history |
| Proven | 2026-10-10, ops slot on `k3d-sf-app` (sf-app ran it, sf-data watched): 21,339 orders replayed, `wait` 1m30s after the restart, 0 duplicate shipments |
| Not for | Re-running `make up` on a running cluster: it keeps the epoch (`cdc-epoch.sh ensure`) and changes nothing |

## Why the connectors cannot just be restarted

Debezium (`snapshot.mode=when_needed`) snapshots only when it has no stored offsets. A restart with its offsets keeps
streaming from the slot and stamps the epoch it read at task start; after `cdc-epoch.sh new` that would leave bronze
on the old epoch while the Secret names the new one. So the source's offsets are reset while it is stopped, and the
sink's tasks are restarted so they pick up the new control topic. The sink's own consumer offsets stay: it keeps
reading the CDC topics where it was.

## Procedure

Set `K="kubectl --context k3d-<cluster>"` (on EKS, the session's context) and `export CLUSTER=<cluster>`. Times are
from the proven run.

1. **New epoch**: `scripts/cdc-epoch.sh new` (prints N; writes Secret `kafka/cdc-epoch` and KafkaTopic
   `iceberg-control-N`; with profile `rt` it also deletes FlinkDeployment `flink/kpi-minute`, which Argo CD
   recreates for epoch N). Running tasks keep the old epoch until step 4.
2. **Stop the source**: `$K -n kafka patch kafkaconnector shop-postgres --type merge -p '{"spec":{"state":"stopped"}}'`.
   Wait until `$K -n kafka get kafkaconnector shop-postgres -o jsonpath='{.status.connectorStatus.connector.state}'`
   is `STOPPED` and stays so (6 s; Argo CD does not revert it: `state` is not in git, the app stays Synced).
3. **Reset its offsets**: `$K -n kafka annotate kafkaconnector shop-postgres strimzi.io/connector-offsets=reset --overwrite`.
   Strimzi removes the annotation when done (6 s); the Connect log shows `DELETE /connectors/shop-postgres/offsets`
   with status 200.
4. **Resume the source**: patch `{"spec":{"state":"running"}}`. The log must show `No previous offsets found` and
   `transforms.epoch.static.value = N`, then `Snapshot step 7 - Snapshotting data`, one `Finished exporting` line per
   table (7 tables: customers, products, orders, order_items, payments, shipments, heartbeat), `Snapshot completed`
   and `Starting streaming` (about 2 s for ~21k orders). Streaming resumes from the slot's position, so a few changes
   from just before the snapshot can be replayed as `c`/`u`; bronze is append-only and consumers must be idempotent.
5. **Restart the sink's tasks**: patch `iceberg-sink` to `stopped`, wait for `STOPPED`, patch back to `running`
   (6 s each). The log then shows `iceberg.control.topic = iceberg-control-N` and the coordinator subscribing to it
   (`Found no committed offset` on that topic is expected). Do not reset the sink's offsets. `strimzi.io/restart=true`
   may restart only the connector, not its tasks; use the state change.
6. **Record the epoch**: `scripts/cdc-epoch.sh wait --timeout 900`. It waits for Debezium's `SnapshotCompleted` and
   for snapshot rows of epoch N in `bronze.heartbeat`, then sets `meta.cdc_epochs.snapshot_completed_at` (1m30s after
   step 4 in the proven run; the sink commits every 60 s). Silver uses epoch N from the next `dbt_build`.

Then rebuild and check: trigger `dbt_build`, and run `reconcile` (see [reconciliation-failure.md](reconciliation-failure.md)).

## If a step does not do what it says

- Step 2 does not reach `STOPPED`, or flips back: do not reset offsets. Check `$K -n kafka describe kafkaconnector
  shop-postgres` (operator events) and the kafka-connect app in Argo CD.
- Step 4 logs a stored offset instead of `No previous offsets found`: step 3 did not take effect. Stop the source again
  and repeat step 3.
- Step 6 times out: the source did not snapshot (step 3/4), or the sink still uses the old control topic (step 5); the
  log of `cdc-connect-0` tells which. Until it succeeds silver keeps reading the previous epoch.
