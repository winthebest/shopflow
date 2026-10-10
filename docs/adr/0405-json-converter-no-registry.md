# 0405. Schemaless JSON on Kafka, the bronze DDL is the schema (no Schema Registry)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Change events go to two consumers: the Iceberg sink (bronze) and Flink SQL (realtime KPIs). Options are Avro or
JSON-with-schema through a Schema Registry, JSON with an inline schema envelope, or plain JSON. Source schema
changes are already gated by data contracts in CI (Phase 5).

## Decision

`JsonConverter` with `schemas.enable=false` for keys and values. Types are carried as follows:

- `numeric` → `decimal.handling.mode=string` (exact), converted to the bronze `decimal(p, s)` column by the sink;
- `timestamptz` → Debezium ISO-8601 string, converted to `timestamp(6) with time zone`;
- `_ingested_at` → Kafka record time (`InsertField` with `timestamp.field` on the sink side; epoch milliseconds).

Record format on `shop.public.*` (value; the key is `{"<pk>": ...}`), observed on the cluster on 2026-10-10:

- every source column of the after-image, plus `_op` (`c|u|d|r`), `_lsn`, `_source_ts_ms` (ExtractNewRecordState
  `add.fields`), `__deleted` (`"true"|"false"`, from `delete.tombstone.handling.mode=rewrite`) and `_cdc_epoch`;
- `_cdc_epoch` is a **string** (`InsertField` static value, e.g. `"1791611151"`, epoch seconds); bronze stores it as
  `bigint`;
- a delete (`_op = 'd'`, `__deleted = "true"`) carries the real key only: Postgres sends just the key (default
  replica identity) and Debezium fills every other NOT NULL column with its type's default (`0`, `""`, `"0.00"`,
  `1970-01-01T00:00:00Z`). Consumers (silver, Flink, services) must trust only the key of a `d` row and decide by
  `_op` (or `__deleted`), never by its other columns;
- no tombstones (`tombstones.on.delete=false`).

Bronze tables are created up front with explicit types (`deploy/platform/trino/base/bronze-tables.sql`), with
`auto-create` and schema evolution off in the sink: unknown fields (for example the routing field `_topic`) are
dropped, and a new source column reaches bronze only through a reviewed DDL change.

## Alternatives considered

| Option | Why not |
|---|---|
| Avro + Schema Registry (Apicurio/Karapace) | One more stateful service on a laptop that already runs near its memory limit; registry-driven evolution bypasses the contract review |
| JSON with schema envelope (`schemas.enable=true`) | ~3× larger messages; Flink SQL must unwrap `payload`; Debezium time types are not Connect logical types anyway |

## Consequences

- Positive: flat JSON is readable with `kafka-console-consumer` and maps directly to Flink SQL columns; one schema
  authority per layer (Alembic for Postgres, bronze DDL for Iceberg).
- Verified in the smoke test: decimal strings (`99.90`) and ISO timestamps land in typed bronze columns.
- Negative / risks: no wire-level type safety; a source type change that the sink cannot convert fails the sink
  task (visible as `connector-failed`), it does not silently corrupt data.
- When to revisit: more producers/consumers of the topics than this pipeline, or binary/large payloads.
