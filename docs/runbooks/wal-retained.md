# Runbook: WAL retained by the Debezium slot

| | |
|---|---|
| Alerts | `PostgresSlotWALRetainedCritical` (page), `PostgresSlotWALRetainedHigh` (ticket), `PostgresSlotInactive` (ticket) |
| Rule | [`deploy/platform/slo/base/wal-retained.prometheusrule.yaml`](../../deploy/platform/slo/base/wal-retained.prometheusrule.yaml) (tests: `slo/tests/wal-retained.test.yaml`) |
| Related | sf-data: [connector-failed.md](connector-failed.md), [cdc-lag.md](cdc-lag.md); [gameday.md](gameday.md) scenario 3 |

## What it means

Debezium reads `shop-db` through the logical replication slot `debezium_shop`. While nothing confirms
progress on the slot, Postgres keeps every WAL segment since the slot's `restart_lsn`. `max_slot_wal_keep_size`
(2GB locally) caps that. Past the cap, Postgres **invalidates the slot** to protect the disk, and CDC can only
continue with a full re-snapshot under a new epoch.

- **Inactive** (ticket): no consumer on the slot for 10 minutes. WAL is starting to pile up.
- **High** (ticket): more than half of the cap is held. Fix within the working session.
- **Critical** (page): past 80%, or at the current rate the cap is reached within the hour. Act now.

Unit-tested timings: at 1 MiB/s of WAL the page fires at ~14 min, 20 minutes before invalidation. At 0.1 MiB/s
the ticket comes at ~3h and the page ~1h before the cap.

## Triage

1. Is Debezium connected? `kubectl -n kafka get kafkaconnector shop-postgres -o jsonpath='{.status.connectorStatus.connector.state}'`
   and the Kafka Connect pods: `kubectl -n kafka get pods -l strimzi.io/cluster=cdc`.
2. Grafana → CNPG dashboard (replication section) or Explore:
   `shopflow:pg_slot_wal_retained:bytes` and `shopflow:pg_slot_wal_retained:ratio`.
3. How fast is it growing? `deriv(shopflow:pg_slot_wal_retained:bytes[15m])` gives bytes/s; the time left is
   (cap − retained) / rate.

## Mitigation

| Situation | Action |
|---|---|
| Connect pods down / crash-looping | Follow [connector-failed.md](connector-failed.md); once the connector runs, the slot advances and the alerts clear |
| Connector paused or failed task | Restart the connector/task (Strimzi annotation `strimzi.io/restart=true` on the KafkaConnector) |
| Cannot recover before the cap | Let Postgres invalidate the slot (do not raise the cap blindly: it only moves the disk risk). Then re-snapshot under a new epoch with `scripts/cdc-epoch.sh` (sf-data), and check the Phase 5 reconciliation |
| Planned stop (game day 3) | Stop the experiment (`kubectl delete -f chaos/gd3-debezium-stopped.yaml`) before retained WAL passes 80% |

## After

Record in the postmortem: time from stop to each alert, retained bytes at the peak, whether the slot survived,
and whether a re-snapshot was needed.
