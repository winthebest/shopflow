# 0506. AWS Glue as the Iceberg catalog on AWS

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

Locally, the Trino catalog `lake` uses an Iceberg REST catalog that lives inside the cluster. On AWS the cluster is
destroyed after every session, but the lakehouse must survive (S3 data plus catalog metadata).

## Decision

On AWS, `lake` uses AWS Glue Data Catalog: Kafka Connect's Iceberg sink and Trino use the Glue catalog with data in
`s3://shopflow-data-<account>/iceberg/`. Layer 0 creates one Glue database per schema (`bronze`, `silver`, `gold`,
from `glue_databases` in the contract) because Trino maps each schema to a Glue database. Pod Identity roles for
Connect and Trino are limited to those databases and the `iceberg/` prefix.

## Alternatives considered

| Option | Why not |
|---|---|
| Run the REST catalog on EKS with its own database | another stateful component to back up and restore every session |
| S3 Tables | managed table maintenance overlaps with what Phases 11–12 build; fewer engines support it |

## Consequences

- Positive: catalog survives sessions at ~$0; no extra restore step.
- Negative: catalog behaviour differs between local and AWS (e.g. dbt `on_table_exists` must be `drop` on Glue, see
  Phase 5 spike); schemas must be added to the contract before use.
- Negative: Trino has one service account, so on AWS `lake` and `lake_ro` share one IAM role with write access;
  read-only for `lake_ro` then rests on the engine (`iceberg.security=READ_ONLY`) alone. Whether to restore a
  storage-level layer is the [open question in ADR 0410](0410-trino-catalogs-per-identity.md#open-question-phase-6-aws),
  to decide in Phase 6 with sf-data.
- When to revisit: if Glue limits break a Phase 9–12 experiment (concurrent commits, maintenance procedures).
