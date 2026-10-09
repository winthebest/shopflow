# 0504. Node group in a single AZ

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

EBS volumes live in one AZ. With nodes in two AZs, a Kafka or Postgres pod rescheduled after a spot interruption or
a pause/resume may land in the other AZ and never attach its volume. Multi-AZ high availability is a non-goal of
this project (one operator, short sessions, restore from backup every session).

## Decision

The managed node group uses only the AZ-a subnet (`node_az` in `infra/cloud-contract.json`); the control plane
still spans two AZs. The gp3 StorageClass (sf-platform, aws overlay) restricts `allowedTopologies` to the same AZ.

## Alternatives considered

| Option | Why not |
|---|---|
| Nodes in two AZs | stuck pods on volume/AZ mismatch; cross-AZ traffic charges |
| One node group per AZ with topology spread | more cost and complexity for an HA goal we do not have |

## Consequences

- Positive: pause/resume and spot replacement always reattach volumes.
- Negative: an AZ outage or a spot shortage in AZ-a stops the session (mitigated by diverse instance types, 0505).
- When to revisit: if HA becomes a goal, or spot capacity in AZ-a is repeatedly unavailable.
