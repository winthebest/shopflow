-- Realtime shop KPIs per minute (plan phase 4, profile rt), from the CDC topics to serving.kpi_minute (Grafana).
--   orders, gmv:           order inserts, by order created_at
--   payment_failure_rate:  failed (declined or error) / all payment inserts, by payment created_at
-- Only inserts (_op = 'c') count: snapshot rows (_op = 'r') never do, so a re-snapshot into a new CDC epoch does not
-- make the KPIs jump. Event time is created_at with a 30 s watermark; rows later than that are dropped (window TVF).
-- A partition without records for 1 minute is marked idle (scan.watermark.idle-timeout), so it does not hold the
-- watermark back: shop.public.orders has 6 partitions and a quiet shop leaves most of them empty.
-- The sink upserts by window_start, so a replay rewrites the same rows.
-- Run by io.shopflow.flink.SqlRunner: placeholders come from the environment, statements end with ';' at line end.

-- One job and one checkpoint directory per CDC epoch: a new epoch starts from fresh state (scripts/cdc-epoch.sh new
-- deletes the FlinkDeployment, Argo CD recreates it) and never restores another epoch's checkpoint.
SET 'pipeline.name' = 'kpi-minute-${CDC_EPOCH}';
SET 'execution.checkpointing.dir' = 's3://lake/flink-ckpt/${CDC_EPOCH}';

CREATE TABLE orders_cdc (
    id BIGINT,
    total STRING,
    created_at STRING,
    `_op` STRING,
    event_time AS CAST(REPLACE(REPLACE(created_at, 'T', ' '), 'Z', '') AS TIMESTAMP(3)),
    WATERMARK FOR event_time AS event_time - INTERVAL '30' SECOND
) WITH (
    'connector' = 'kafka',
    'topic' = 'shop.public.orders',
    'properties.bootstrap.servers' = 'shopflow-kafka-bootstrap.kafka.svc:9093',
    'properties.group.id' = 'flink-kpi-minute-${CDC_EPOCH}',
    'properties.security.protocol' = 'SASL_SSL',
    'properties.sasl.mechanism' = 'SCRAM-SHA-512',
    'properties.sasl.jaas.config' = 'org.apache.flink.kafka.shaded.org.apache.kafka.common.security.scram.ScramLoginModule required username="${KAFKA_USER}" password="${KAFKA_PASSWORD}";',
    'properties.ssl.truststore.type' = 'PEM',
    'properties.ssl.truststore.location' = '/etc/kafka-ca/ca.crt',
    'scan.startup.mode' = 'group-offsets',
    'properties.auto.offset.reset' = 'earliest',
    'scan.watermark.idle-timeout' = '1 min',
    'format' = 'json',
    'json.fail-on-missing-field' = 'false',
    'json.ignore-parse-errors' = 'false'
);

CREATE TABLE payments_cdc (
    id BIGINT,
    status STRING,
    created_at STRING,
    `_op` STRING,
    event_time AS CAST(REPLACE(REPLACE(created_at, 'T', ' '), 'Z', '') AS TIMESTAMP(3)),
    WATERMARK FOR event_time AS event_time - INTERVAL '30' SECOND
) WITH (
    'connector' = 'kafka',
    'topic' = 'shop.public.payments',
    'properties.bootstrap.servers' = 'shopflow-kafka-bootstrap.kafka.svc:9093',
    'properties.group.id' = 'flink-kpi-minute-${CDC_EPOCH}',
    'properties.security.protocol' = 'SASL_SSL',
    'properties.sasl.mechanism' = 'SCRAM-SHA-512',
    'properties.sasl.jaas.config' = 'org.apache.flink.kafka.shaded.org.apache.kafka.common.security.scram.ScramLoginModule required username="${KAFKA_USER}" password="${KAFKA_PASSWORD}";',
    'properties.ssl.truststore.type' = 'PEM',
    'properties.ssl.truststore.location' = '/etc/kafka-ca/ca.crt',
    'scan.startup.mode' = 'group-offsets',
    'properties.auto.offset.reset' = 'earliest',
    'scan.watermark.idle-timeout' = '1 min',
    'format' = 'json',
    'json.fail-on-missing-field' = 'false',
    'json.ignore-parse-errors' = 'false'
);

CREATE TABLE kpi_minute (
    window_start TIMESTAMP(3),
    window_end TIMESTAMP(3),
    orders BIGINT,
    gmv DECIMAL(14, 2),
    payments BIGINT,
    failed_payments BIGINT,
    payment_failure_rate DOUBLE,
    PRIMARY KEY (window_start) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = 'jdbc:postgresql://shop-db-rw.shop.svc:5432/serving?sslmode=require',
    'table-name' = 'kpi_minute',
    'username' = '${SERVING_USER}',
    'password' = '${SERVING_PASSWORD}'
);

CREATE TEMPORARY VIEW shop_events AS
SELECT event_time, 1 AS is_order, CAST(total AS DECIMAL(12, 2)) AS amount, 0 AS is_payment, 0 AS is_failed
FROM orders_cdc
WHERE `_op` = 'c'
UNION ALL
SELECT event_time, 0, CAST(0 AS DECIMAL(12, 2)), 1, CASE WHEN status <> 'succeeded' THEN 1 ELSE 0 END
FROM payments_cdc
WHERE `_op` = 'c';

INSERT INTO kpi_minute
SELECT
    window_start,
    window_end,
    CAST(SUM(is_order) AS BIGINT) AS orders,
    CAST(SUM(amount) AS DECIMAL(14, 2)) AS gmv,
    CAST(SUM(is_payment) AS BIGINT) AS payments,
    CAST(SUM(is_failed) AS BIGINT) AS failed_payments,
    CASE WHEN SUM(is_payment) = 0 THEN CAST(NULL AS DOUBLE)
        ELSE CAST(SUM(is_failed) AS DOUBLE) / SUM(is_payment) END AS payment_failure_rate
FROM TABLE(TUMBLE(TABLE shop_events, DESCRIPTOR(event_time), INTERVAL '1' MINUTE))
GROUP BY window_start, window_end;
