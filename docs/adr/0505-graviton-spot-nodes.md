# 0505. Graviton spot instances for nodes

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

Nodes are the biggest variable cost. Images are multi-arch (`linux/arm64` + `linux/amd64`). Sessions are short and
every stateful component restores from S3, so an interruption is a game-day scenario rather than data loss.

## Decision

One managed node group, `capacity_type = SPOT`, AMI `AL2023_ARM_64_STANDARD`, several Graviton types with ~16 GB
(`m7g.xlarge`, `m6g.xlarge`, `r7g.large`, `r6g.large`, `c7g.2xlarge`), min 0. The final list follows the RAM measured
on k3d in Phases 3–5. Types must come from `allowed_instance_types` in the contract, which is also the operator's
`ec2:RunInstances` allow-list.

## Alternatives considered

| Option | Why not |
|---|---|
| On-demand x86 | about 3–4x the hourly price for the same memory |
| Fargate | no EBS for Kafka/Postgres; per-pod pricing higher at this size |
| A single instance type | spot shortages in one AZ would stop the session |

## Consequences

- Positive: lowest $/GB-hour; spot interruptions double as chaos tests (Phase 7).
- Negative: arm64-only images would break nodes (CI builds multi-arch); a shortage in AZ-a delays sessions. Last
  resort is one on-demand node, with its cost recorded in `docs/cost.md`.
- When to revisit: if the node group stays pending or `CREATE_FAILED` for the listed types.
