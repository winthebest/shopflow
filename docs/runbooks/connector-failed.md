# Runbook: CdcConnectorFailed

| | |
|---|---|
| Alert | `CdcConnectorFailed` (`severity=ticket`), per connector, after 10 minutes |
| Rule | [`deploy/platform/slo/base/cdc-sli.prometheusrule.yaml`](../../deploy/platform/slo/base/cdc-sli.prometheusrule.yaml) |
| Signal | `kafka_connect_connector_task_status` from the KafkaConnect `cdc` metrics (port 9404, PodMonitor `cdc-connect`) |

## What it means

KafkaConnector `shop-postgres` (Debezium) or `iceberg-sink` has had no task in state `running` for 10 minutes
(failed, unassigned, paused, or the metric is gone). Strimzi auto-restarts failed connectors with a growing
back-off, so a task that stays down needs a look. A stopped source also burns the `cdc-lag` SLO
([cdc-lag](cdc-lag.md)) and lets the replication slot retain WAL.

## Triage

The Connect worker runs in a StrimziPodSet pod:
`CONNECT_POD=$(kubectl -n kafka get pod -l strimzi.io/cluster=cdc,strimzi.io/kind=KafkaConnect -o name | head -1)`.

1. Status and stack trace:
   `kubectl -n kafka get kafkaconnector <name> -o jsonpath='{.status.connectorStatus}'`, or
   `kubectl -n kafka exec "$CONNECT_POD" -- curl -s localhost:8083/connectors/<name>/status`.
2. Common causes, by connector:
   - `shop-postgres`: Postgres unreachable or credentials (Secret `shop/shop-db-debezium`, Role
     `cdc-connect-debezium-db`); publication `shop_cdc` or slot `debezium_shop` missing (Alembic, CNPG);
     heartbeat query denied (needs `UPDATE` on `heartbeat`); topic missing (auto-creation is off: KafkaTopic CRs in
     `deploy/platform/kafka/base/topics.yaml`); `cdc-epoch` Secret missing (`scripts/cdc-epoch.sh new`).
   - `iceberg-sink`: control topic `iceberg-control-<epoch>` missing (`scripts/cdc-epoch.sh new`); catalog
     (Polaris, Secret `polaris-iceberg-sink`) or storage (SeaweedFS, Secret `lake-s3-iceberg-sink`) errors; a bronze
     table missing (Trino Job `trino-bronze-tables`); a source type that no longer converts (data contract change
     without a bronze DDL change: `make data-validate` catches it in CI); 10 consecutive commit failures.
   - Kafka authorization errors (`TopicAuthorizationException`, `GroupAuthorizationException`): KafkaUser ACLs in
     `deploy/platform/kafka/base/users.yaml`.
3. Worker healthy? `kubectl -n kafka get pods -l strimzi.io/cluster=cdc` and its logs.

## Mitigation

Fix the cause, then restart the task:
`kubectl -n kafka annotate kafkaconnector <name> strimzi.io/restart-task=0 --overwrite`.
The source resumes from its stored offsets (the slot keeps the WAL); the sink resumes from its committed offsets
without duplicates in a commit. If the slot or offsets are gone, follow the re-snapshot steps in
[cdc-lag](cdc-lag.md).
