# 0403. Kafka on Strimzi in both environments (not Amazon MSK)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

The platform runs on k3d locally and on ephemeral EKS (Phase 6) from the same GitOps manifests, with a $100 AWS
budget. Kafka must support TLS + SCRAM, ACLs per identity, topics as code, and a fresh cluster per `make up` /
`cloud-up` (the CDC epoch design assumes a new Kafka can appear at any time, docs/adr/0406).

## Decision

Run Kafka with the Strimzi operator everywhere: `KafkaNodePool` + `Kafka` (KRaft), `KafkaUser` with simple ACLs,
`KafkaTopic` CRs, `KafkaConnect` + `KafkaConnector`. Locally one dual-role node with replication factor 1; the
`aws` overlay adds 3 brokers, RF=3, `min.insync.replicas=2`.

## Alternatives considered

| Option | Why not |
|---|---|
| Amazon MSK (provisioned or serverless) | No free tier, billed per hour even when idle: a large share of the $100 budget; different manifests per environment; IAM auth instead of SCRAM ACLs |
| Redpanda | Business Source License, not OSS |
| Bitnami Kafka chart | Bitnami images moved to a frozen legacy repository in 2025 |

## Consequences

- Positive: identical manifests and security model locally and on EKS; users, ACLs and topics reviewed in git.
- Negative / risks: we operate Kafka ourselves (upgrades, storage); one more operator in the `data` profile
  (memory limits: 384Mi operator, 1Gi local broker).
- When to revisit: if the cloud part becomes long-running instead of ephemeral sessions.
