-- Bronze: one append-only Iceberg table per CDC source table (docs/adr/0406, 0407).
-- Source columns mirror the Alembic schema (services/orders/migrations; Iceberg has no smallint, so heartbeat.id
-- is integer); all nullable. In a delete row (_op = 'd') only the key is real: Debezium fills the other columns with
-- type defaults (0, '', 1970-01-01; docs/adr/0405), so readers decide by _op, never by those values. Metadata columns:
--   _op            c | u | d | r (r = snapshot read)       _lsn           Postgres LSN, comparable within one epoch
--   _source_ts_ms  source commit time (epoch ms)           _cdc_epoch     CDC epoch (new Kafka/Postgres life)
--   _ingested_at   Kafka record time, the partition key
-- format_version = 2: Trino 483 cannot OPTIMIZE/DELETE/MERGE v3 tables. Idempotent: safe to re-run.

CREATE SCHEMA IF NOT EXISTS lake.bronze;

CREATE TABLE IF NOT EXISTS lake.bronze.customers (
    id            bigint,
    email         varchar,
    name          varchar,
    created_at    timestamp(6) with time zone,
    updated_at    timestamp(6) with time zone,
    _op           varchar,
    _lsn          bigint,
    _source_ts_ms bigint,
    _cdc_epoch    bigint,
    _ingested_at  timestamp(6) with time zone
)
WITH (format_version = 2, partitioning = ARRAY['day(_ingested_at)']);

CREATE TABLE IF NOT EXISTS lake.bronze.products (
    id            bigint,
    sku           varchar,
    name          varchar,
    price         decimal(12, 2),
    created_at    timestamp(6) with time zone,
    updated_at    timestamp(6) with time zone,
    _op           varchar,
    _lsn          bigint,
    _source_ts_ms bigint,
    _cdc_epoch    bigint,
    _ingested_at  timestamp(6) with time zone
)
WITH (format_version = 2, partitioning = ARRAY['day(_ingested_at)']);

CREATE TABLE IF NOT EXISTS lake.bronze.orders (
    id            bigint,
    customer_id   bigint,
    status        varchar,
    total         decimal(12, 2),
    created_at    timestamp(6) with time zone,
    updated_at    timestamp(6) with time zone,
    _op           varchar,
    _lsn          bigint,
    _source_ts_ms bigint,
    _cdc_epoch    bigint,
    _ingested_at  timestamp(6) with time zone
)
WITH (format_version = 2, partitioning = ARRAY['day(_ingested_at)']);

CREATE TABLE IF NOT EXISTS lake.bronze.order_items (
    id            bigint,
    order_id      bigint,
    product_id    bigint,
    quantity      integer,
    unit_price    decimal(12, 2),
    created_at    timestamp(6) with time zone,
    updated_at    timestamp(6) with time zone,
    _op           varchar,
    _lsn          bigint,
    _source_ts_ms bigint,
    _cdc_epoch    bigint,
    _ingested_at  timestamp(6) with time zone
)
WITH (format_version = 2, partitioning = ARRAY['day(_ingested_at)']);

CREATE TABLE IF NOT EXISTS lake.bronze.payments (
    id            bigint,
    order_id      bigint,
    amount        decimal(12, 2),
    status        varchar,
    provider_ref  varchar,
    created_at    timestamp(6) with time zone,
    updated_at    timestamp(6) with time zone,
    _op           varchar,
    _lsn          bigint,
    _source_ts_ms bigint,
    _cdc_epoch    bigint,
    _ingested_at  timestamp(6) with time zone
)
WITH (format_version = 2, partitioning = ARRAY['day(_ingested_at)']);

-- shipments: one row per paid order, written by the fulfillment worker (Phase 7). cdc_epoch / source_lsn are the
-- worker's own record of the order change it acted on; _cdc_epoch / _lsn are this row's CDC metadata.
CREATE TABLE IF NOT EXISTS lake.bronze.shipments (
    id            bigint,
    order_id      bigint,
    cdc_epoch     integer,
    source_lsn    bigint,
    created_at    timestamp(6) with time zone,
    updated_at    timestamp(6) with time zone,
    _op           varchar,
    _lsn          bigint,
    _source_ts_ms bigint,
    _cdc_epoch    bigint,
    _ingested_at  timestamp(6) with time zone
)
WITH (format_version = 2, partitioning = ARRAY['day(_ingested_at)']);
CREATE TABLE IF NOT EXISTS lake.bronze.heartbeat (
    id            integer,
    beat_at       timestamp(6) with time zone,
    _op           varchar,
    _lsn          bigint,
    _source_ts_ms bigint,
    _cdc_epoch    bigint,
    _ingested_at  timestamp(6) with time zone
)
WITH (format_version = 2, partitioning = ARRAY['day(_ingested_at)']);
