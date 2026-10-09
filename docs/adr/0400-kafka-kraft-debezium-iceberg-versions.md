# 0400. Kafka 4.3 on Strimzi 1.2 (KRaft), Debezium 3.7, Iceberg 1.12 sink

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Phase 4 needs a CDC chain whose pieces are maintained in 2026 and work together: an operator for Kafka 4.x (KRaft
only, ZooKeeper is gone), a GA Debezium 3.x Postgres connector, and the Apache Iceberg Kafka Connect sink. Release
facts checked on 2026-10-09 from the upstream release pages and repositories:

| Piece | Latest stable | Notes |
|---|---|---|
| Strimzi | 1.2.0 (2026-08-20) | Kafka 4.2.0, 4.2.1, 4.3.0, 4.3.1. Since 1.0.0 only the `kafka.strimzi.io/v1` CRD API exists (v1beta2 removed); KafkaConnect needs `groupId` and the three storage topics in `spec` |
| Kafka | 4.3.1 | image `quay.io/strimzi/kafka:1.2.0-kafka-4.3.1`. The 1.2.0 release notes swap the 4.3.0/4.3.1 digests; the pinned digest is the one whose image label says `1.2.0-4.3.1` |
| Debezium Postgres | 3.7.0.Final (2026-09-29) | Kafka Connect 3.1+, Java 17+, PostgreSQL 14–18, `pgoutput` |
| Iceberg | 1.12.0 (2026-09-30) | first release with `iceberg.control.commit.max-consecutive-failures` (absent in 1.11.0 and 1.10.2) and several coordinator fixes (#17713, #17552, #17933) |

The Iceberg sink is compiled against Kafka 3.9 and reflects into `WorkerSinkTaskContext.consumer`; that field still
exists in Kafka 4.3.1, and the end-to-end smoke test (`images/kafka-connect/smoke`) runs the sink on Connect 4.3.1.

## Decision

Pin Strimzi 1.2.0 with Kafka 4.3.1 (`metadataVersion: 4.3-IV0`), Debezium Postgres 3.7.0.Final and the Iceberg
Kafka Connect runtime built from the Apache Iceberg 1.12.0 source release. All images by digest, all downloads by
sha512 (docs/adr/0401).

## Alternatives considered

| Option | Why not |
|---|---|
| Strimzi 1.3.0 (rc1 on 2026-10-05) | Release candidate |
| Kafka 4.2.x | No reason to start a new cluster one minor behind |
| Debezium 3.6.3.Final | Older patch line with no feature we need that 3.7 lacks; kept as the fallback if 3.7.0 regresses |
| Iceberg 1.11.0 / 1.10.2 | No `max-consecutive-failures`: one commit conflict (e.g. with OPTIMIZE) stops the coordinator |
| Confluent's (Tabular) Iceberg sink | Superseded by the Apache sink; not maintained as a separate project |

## Consequences

- Positive: every version is current and verified together by the smoke test; CRDs are the long-term v1 API.
- Negative / risks: Debezium 3.7.0 and Iceberg 1.12.0 are .0 releases about ten days old; the Iceberg sink is
  not tested upstream on Kafka 4.x (only our smoke test covers it).
- When to revisit: a Strimzi release drops Kafka 4.3, a Debezium/Iceberg patch release fixes a bug we hit, or the
  smoke test fails after a bump.
