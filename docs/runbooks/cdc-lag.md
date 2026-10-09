# Runbook: CdcLagBurn

| | |
|---|---|
| Alert | `CdcLagBurn`: `severity=page` (1h/5m or 6h/30m burn), `severity=ticket` (1d/2h or 3d/6h burn) |
| SLO | `cdc-lag`: 99% of minutes, Postgres changes reach Iceberg bronze within 5 minutes ([`slo/cdc.yaml`](../../slo/cdc.yaml)) |
| SLI | `cdc:bronze_heartbeat_stale:minute` ([`cdc-sli.prometheusrule.yaml`](../../deploy/platform/slo/base/cdc-sli.prometheusrule.yaml)) |

## What it means

Bronze is falling behind Postgres. A minute is bad when `bronze.heartbeat` is 5 minutes or more older than the
last change Debezium read (`data_freshness_seconds{table="bronze.heartbeat"} >= 300`), when the freshness probe
fails, or when the exporter is gone. The heartbeat row is updated every 10s by Debezium itself, so the SLI keeps
moving when the shop is idle: a healthy pipeline sits at about one sink commit interval (60s).

The path is: Postgres WAL → slot `debezium_shop` → KafkaConnector `shop-postgres` → topics `shop.public.*` →
KafkaConnector `iceberg-sink` → Polaris (catalog `lake`) + SeaweedFS → `bronze.*`.

## Triage (follow the path, stop at the first broken hop)

The Connect worker runs in a StrimziPodSet pod:
`CONNECT_POD=$(kubectl -n kafka get pod -l strimzi.io/cluster=cdc,strimzi.io/kind=KafkaConnect -o name | head -1)`.

1. **Is it the measurement?** If `CdcFreshnessProbeDown` also fires, start with
   [freshness-probe-down](freshness-probe-down.md): the minutes are bad because nobody can measure them.
2. **Connectors running?** `kubectl -n kafka get kafkaconnector` (both `Ready`), or the alert
   [`CdcConnectorFailed`](connector-failed.md). Task errors:
   `kubectl -n kafka exec "$CONNECT_POD" -- curl -s localhost:8083/connectors/<name>/status`.
3. **Source side behind?** In Grafana Explore:
   `debezium_metrics_millisecondsbehindsource{context="streaming"}`. Large and growing → Debezium is slow or
   blocked (Postgres load, slot lag; see the WAL-retained alert of sf-sre). Near zero → the source keeps up and the
   delay is in the sink.
4. **Sink committing?** Iceberg commits every 60s. In Trino (`lake_ro`, user `exporter`):
   `SELECT committed_at FROM bronze."heartbeat$snapshots" ORDER BY committed_at DESC LIMIT 3`. No new snapshots →
   sink logs: `kubectl -n kafka logs "$CONNECT_POD" | grep -iE 'iceberg|commit' | tail -50`.
   Typical causes: catalog unreachable (Polaris pod, credentials Secret `polaris-iceberg-sink`), SeaweedFS full or
   down, a commit conflict with maintenance (the sink retries up to 10 consecutive failures).
5. **Kafka healthy?** [`KafkaBrokerDown`](kafka-broker-down.md); consumer lag of group `connect-iceberg-sink`.

## Mitigation

- Restart a failed task: `kubectl -n kafka annotate kafkaconnector <name> strimzi.io/restart-task=0 --overwrite`.
- Catalog or storage down: fix that component first; the sink resumes from its committed offsets without loss
  (exactly-once commits, docs/adr/0406).
- If the slot or the Kafka data is lost, re-snapshot under a new epoch (`scripts/cdc-epoch.sh new`, restart the
  connectors, `scripts/cdc-epoch.sh wait`): bronze keeps the old epoch, silver switches to the new one once its
  snapshot completed (`meta.cdc_epochs.snapshot_completed_at`; allow one more sink commit interval before
  trusting it, the snapshot is complete in Debezium before it is committed to bronze).

While the alert fires, downstream data (silver, gold, dashboards) is stale by at least the reported lag.
