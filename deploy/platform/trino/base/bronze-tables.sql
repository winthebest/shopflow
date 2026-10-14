-- Bronze: one append-only Iceberg table per CDC source table (docs/adr/0406, 0407).
-- Source columns mirror the Alembic schema (services/orders/migrations; Iceberg has no smallint, so heartbeat.id
-- is integer); all nullable. In a delete row (_op = 'd') only the key is real: Debezium fills the other columns with
-- type defaults (0, '', 1970-01-01; docs/adr/0405), so readers decide by _op, never by those values. Metadata columns:
--   _op            c | u | d | r (r = snapshot read)       _lsn           Postgres LSN, comparable within one epoch
--   _source_ts_ms  source commit time (epoch ms)           _cdc_epoch     CDC epoch (new Kafka/Postgres life)
--   _ingested_at   Kafka record time, the partition key
-- format_version = 2: Trino 483 cannot OPTIMIZE/DELETE/MERGE v3 tables. Idempotent: safe to re-run.

CREATE SCHEMA IF NOT EXISTS lake.bronze;

-- dbt's schemas, created once here before any model runs. Each Cosmos task is its own `dbt run` that creates its
-- schema if missing, and Trino's IF NOT EXISTS checks, then creates: on a fresh cluster the parallel silver models
-- raced it, and the losers failed with Polaris's "Namespace already exists". Ordered before the models without new
-- coupling: dbt_build's cdc_epoch_guard needs a completed epoch, which scripts/cdc-epoch.sh records only once
-- bronze.heartbeat holds its snapshot rows, and that table exists only after this file ran (it stops at an error).
CREATE SCHEMA IF NOT EXISTS lake.silver;
CREATE SCHEMA IF NOT EXISTS lake.gold;

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
    idempotency_key varchar,
    request_hash  varchar,
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

-- Columns added to a source table after its bronze table existed. CREATE TABLE IF NOT EXISTS never changes an
-- existing table, and the sink drops fields bronze lacks (iceberg.tables.evolve-schema-enabled=false), so every
-- addition is also an ADD COLUMN IF NOT EXISTS here (a no-op on a new cluster, where CREATE already has it). Keep
-- comments out of the CREATE bodies: the Connect smoke test parses them (create-bronze-tables.py).
-- orders.idempotency_key, orders.request_hash: checkout idempotency (Alembic revision 0006); opaque client tokens
-- and a SHA-256 of the request, not used by silver or gold.
ALTER TABLE lake.bronze.orders ADD COLUMN IF NOT EXISTS idempotency_key varchar;
ALTER TABLE lake.bronze.orders ADD COLUMN IF NOT EXISTS request_hash varchar;
