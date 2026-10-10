# 0415. Realtime KPIs with Flink SQL on the Flink Kubernetes Operator, job baked into one image

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

The plan (phase 4, profile `rt`) wants per-minute shop KPIs (orders, GMV, payment failure rate) from the CDC topics in
a `serving` Postgres table that Grafana reads, independent of the hourly batch path. The KPIs must not jump when
Debezium snapshots again into a new CDC epoch (docs/adr/0406), and a checkpoint must never be restored into another
epoch.

## Decision

- **Flink 2.2 SQL, application mode, Flink Kubernetes Operator 1.16** (namespace `flink`, profile `rt`).
- **Image `shopflow-flink`** (`data/flink/Dockerfile`, built and planned in CI, published by `data-images.yml`):
  - the Kafka SQL connector 5.0.0-2.2 (shaded Kafka client: the SCRAM login module is the shaded class name);
  - the JDBC connector 4.0.0-2.0 with its Postgres dialect plus the Postgres driver;
  - the Hadoop S3A filesystem plugin for checkpoints on SeaweedFS (credentials from the pod environment, not the
    Flink configuration);
  - a 70-line `SqlRunner`, compiled against Flink's own jars;
  - the SQL `data/flink/kpi_minute.sql`.
  Every jar is pinned by sha256, cross-checked against Maven Central's sha1. The SQL is baked in rather than mounted
  from a ConfigMap, for the same reason as the Airflow DAGs (docs/adr/0414): job, connectors and SQL ship as one
  tested artifact. A kustomize ConfigMap generator also cannot read `data/` from a component directory.
- **SqlRunner:**
  - Runs the script as one job: every INSERT goes into one statement set.
  - Fills `${NAME}` placeholders (credentials, the CDC epoch) from the environment, with SQL quotes escaped; a
    missing variable fails.
  - `--explain` plans instead of submitting. CI runs it in the image, so connectors that do not load or invalid SQL
    fail the PR.
- **Semantics:**
  - Only inserts count (`_op = 'c'`; snapshot rows `r` never do).
  - Event time is `created_at` with a 30-second watermark; late rows are dropped by the window TVF. A partition
    without records for 1 minute is idle (`scan.watermark.idle-timeout`) and does not hold the watermark back:
    `shop.public.orders` has 6 partitions (Phase 7 fulfillment worker) and a quiet shop leaves most of them empty.
  - 1-minute tumbling windows over a union of order and payment inserts.
  - The JDBC sink upserts on `window_start`, so replays rewrite the same rows.
- **Offsets:** `scan.startup.mode = group-offsets` with `properties.auto.offset.reset = earliest` on both sources.
  For group-offsets, connector 5.0.0 reads the reset strategy from `auto.offset.reset` and defaults to `NONE`
  (`KafkaDynamicSource`, `case GROUP_OFFSETS`), so a group without committed offsets would fail the job. A new group
  replays the retained topics (the CDC topics are shared by all epochs, only the Iceberg control topic is per epoch):
  snapshot rows are filtered out and the upsert rewrites earlier minutes with the same values, except the oldest
  retained minute. Retention deletes whole log segments, so that minute may be only partly left, and the upsert then
  overwrites its complete value with a smaller one. Locally and on AWS each session starts with a new Kafka, so this
  does not happen in the demo; it would matter for a long-lived Kafka.
- **Epochs:** the Kafka consumer group (`flink-kpi-minute-<epoch>`), the job name and the checkpoint directory
  (`SET 'execution.checkpointing.dir' = 's3://lake/flink-ckpt/<epoch>'`, applied by SqlRunner to the job's
  configuration) all carry the CDC epoch. On a new epoch, `scripts/cdc-epoch.sh new` deletes the FlinkDeployment
  (the operator drops its HA state with it) and Argo CD recreates it: the job starts from fresh state and never
  restores another epoch's checkpoint.
- **Deployment (profile `rt`):**
  - App `flink-operator` (wave 0): the operator chart from `dlcdn.apache.org`, watching namespace `flink` only.
  - App `flink` (wave 1), `deploy/platform/flink/`:
    - an in-cluster copy Job (`copy-secret.py`, as in the other data components) brings the Kafka user, the cluster
      CA, the CDC epoch and the `flink_serving` role into namespace `flink`;
    - a DDL Job creates `serving.kpi_minute` as `flink_serving` and grants `SELECT` to `grafana_serving`;
    - FlinkDeployment `kpi-minute` (`upgradeMode: last-state`).
  - Checkpoints and HA state go to SeaweedFS under `lake/flink-ckpt/`, with S3 identity `flink`, which can write
    only below that prefix.
  - Grafana: the same copy Job writes Secret `observability/grafana-serving-datasource` (label
    `grafana_datasource=1`, one key `serving.yaml`, datasource uid `serving` as `grafana_serving`, `sslmode:
    require`). The password is filled into the provisioning file as a JSON string (`DERIVED_ENCODING=json`). The
    sidecar then loads the datasource once, complete, at the moment the password exists; a `$__file{}` reference
    to an optional mount could be read while still empty on the first `rt` sync. Dashboard "Shopflow / KPI –
    realtime" (folder Shopflow, refresh 30s, every query bounded by `$__timeFilter`).

## Alternatives considered

| Option | Why not |
|---|---|
| RisingWave | The plan's fallback; Flink is the more common streaming engine to show |
| Flink session cluster + `sql-client.sh -f` Job | Job lifecycle (upgrade, checkpoints) outside the operator |
| SQL from a ConfigMap | SQL and connector versions could drift apart; kustomize cannot load files outside the component |
| JDBC connector 4.1.0-2.2 | Registers an OpenLineage extractor whose client library (plus Jackson and a native SQL parser) is not bundled: `NoClassDefFoundError` at planning |
| PyFlink | Python and the PyFlink package make the image much larger for a few SQL statements |

## Consequences

- Positive: a Debezium re-snapshot cannot inflate the KPIs; the KPI path keeps working when Trino and Airflow are
  scaled down (profile `rt` alone with `data`).
- Negative / risks:
  - JDBC 4.0.0-2.0 was built against Flink 2.0. CI plans the job on 2.2, but only the cluster run proves the sink
    at runtime.
  - Counting by insert assumes orders and payments get their final amount and status at insert, which is how the
    shop writes them (services/orders).
  - Argo CD does not manage the datasource Secret: it stays in `observability` when profile `rt` is removed (a
    read-only datasource whose dashboard is empty). A new `grafana_serving` password reaches Grafana on the next
    run of the copy Job, i.e. a sync with hooks of app `flink` (docs/runbooks/data-setup-hooks.md, step 4).
- When to revisit: a JDBC connector release for 2.2 without the lineage dependency, or KPIs that need updates and
  deletes (would require a changelog source instead of filtering inserts).
