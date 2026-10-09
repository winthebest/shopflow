# 0101. Data contracts for CDC source tables, checked in CI against the migrated schema

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-app

## Context

The shop schema (owned by Alembic, sf-app) is also the input of the data platform: Debezium publishes six tables
(`shop_cdc`) into Kafka, and the Iceberg bronze/silver models (sf-data) read their columns. A migration that drops,
renames or narrows a column, or makes a column NOT NULL, passes every app test and then breaks the pipeline
downstream, possibly hours later. The producer and the consumers are different lanes, so the schema they agree on
has to be written down and enforced where the producer changes it: in the PR.

## Decision

One YAML contract per published table in `data/contracts/<table>.yaml` (version, columns with Postgres type and
nullability, primary key). `scripts/check_contracts.py`, run by the `contracts` job in `ci.yml`, migrates a real
Postgres with Alembic, introspects it, and fails when:

1. the tables in publication `shop_cdc` differ from the contract files;
2. the migrated schema differs from a contract (the contract must describe the schema exactly);
3. compared with the contracts on the PR base (or the previous `main` commit), a breaking change (column dropped
   or renamed, type not widened, nullable → NOT NULL, primary key changed, contract removed) arrives without a
   `version` bump.

Non-breaking changes (new column, widened type, NOT NULL → nullable) only need the contract updated.

## Alternatives considered

| Option | Why not |
|---|---|
| Schema registry (Avro/Protobuf) on the Kafka side | Catches the break only after the migration is deployed; Phase 4 uses JSON without a registry |
| Compare SQLAlchemy models with contracts | Models are not the schema: Alembic is (`alembic check` keeps them in sync, but the database is the truth) |
| dbt source tests only | Run in the consumer pipeline, after the producer already shipped the change |
| Diff Alembic migration files | Hard to interpret reliably (raw SQL, data migrations); the migrated schema is unambiguous |

## Consequences

- Positive: a breaking schema change is visible in the producer's PR, names the table and column, and forces an
  explicit version bump that consumers can react to; adding a source table requires a contract (publication and
  contract files must match).
- Negative / risks: the type lattice is deliberately small (integer family, numeric precision/scale, varchar
  length, varchar → text); any other type change counts as breaking, which can be a false positive. Removing a
  source table always fails and needs a coordinated, manually reviewed change.
- When to revisit: if consumers need semantic rules (allowed enum values, value ranges), or if a schema registry
  is introduced for the Kafka topics; then generate contracts from one source instead of keeping two.
