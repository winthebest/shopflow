-- serving.kpi_minute: the per-minute KPIs the Flink job upserts on window_start (data/flink/kpi_minute.sql), read
-- by Grafana as grafana_serving. Run as flink_serving, the owner of database serving. Idempotent.
CREATE TABLE IF NOT EXISTS kpi_minute (
    window_start         timestamp(3) PRIMARY KEY,
    window_end           timestamp(3) NOT NULL,
    orders               bigint NOT NULL,
    gmv                  numeric(14, 2) NOT NULL,
    payments             bigint NOT NULL,
    failed_payments      bigint NOT NULL,
    payment_failure_rate double precision
);
GRANT SELECT ON kpi_minute TO grafana_serving;
