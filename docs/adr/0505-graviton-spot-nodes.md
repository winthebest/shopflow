# 0505. Graviton spot instances for nodes

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

Nodes are the biggest variable cost. Images are multi-arch (`linux/arm64` + `linux/amd64`). Sessions are short and
every stateful component restores from S3, so an interruption is a game-day scenario rather than data loss.

## Decision

One managed node group, `capacity_type = SPOT`, AMI `AL2023_ARM_64_STANDARD`, min 0. The types are
`node_instance_types` in `infra/cloud-contract.json` (layer 2 reads them from there). They must also be in
`allowed_instance_types`, which is the operator's `ec2:RunInstances` allow-list.

The rule is **16 GiB per node, and a spot interruption rate of 10% or less in ap-southeast-1**, with at least
two types for capacity. Figures are from the Spot Instance Advisor and the public spot price feed, 2026-10-09:

| Type | vCPU | Spot $/h | Interruption | In the list |
|---|---|---|---|---|
| r8g.large | 2 | 0.048 | < 5% | yes |
| r6g.large | 2 | 0.059 | 5–10% | yes |
| m8g.xlarge | 4 | 0.112 | 5–10% | yes |
| c6g.2xlarge | 8 | 0.144 | < 5% | yes |
| c7g.2xlarge | 8 | 0.165 | < 5% | yes |
| r7g.large | 2 | 0.051 | 10–15% | no |
| m6g.xlarge / m7g.xlarge | 4 | 0.068 / 0.091 | > 20% | no |

EKS picks spot pools by price and capacity, not by list order, so the list itself is the preference: a type with
frequent interruptions is left out rather than placed last.

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
- When to revisit:
  - the node group stays pending or `CREATE_FAILED` for the listed types;
  - the CPU requests measured on k3d (Phases 3–5) exceed what two 2-vCPU nodes give: then drop the 2-vCPU
    types (r8g.large, r6g.large);
  - the advisor's interruption rates move.
