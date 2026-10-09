# 0503. Public subnets, no NAT gateway

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

A NAT gateway costs about $0.06/h plus data processing, more than the EKS control plane, for every hour of every
session, and it must be torn down and recreated or kept forever. Nodes need outbound access (images from GHCR,
AWS APIs).

## Decision

Layer 1 has two public subnets (two AZs, required by the EKS control plane), an internet gateway and no NAT. Nodes
get public IPv4 addresses. Inbound traffic is still closed: nodes use the EKS cluster security group (members
only) plus the rules the LB controller adds for the NLB; the NLB accepts only the operator's IP; the EKS public API
endpoint accepts only the operator's /32. The operator role is denied `ec2:CreateNatGateway` and
`ec2:AllocateAddress`.

## Alternatives considered

| Option | Why not |
|---|---|
| Private subnets + NAT gateway | ~$0.06/h + data, the largest fixed cost of a session |
| Private subnets + VPC endpoints | interface endpoints cost ~$0.01/h each per AZ and still miss GHCR |
| NAT instance | another EC2 to patch and keep alive |

## Consequences

- Positive: no fixed networking cost; layer 1 is free and permanent.
- Negative: public IPv4 addresses cost $0.005/h each; nodes are reachable at L3 if a security group is opened by
  mistake (trivy AWS-0164 is ignored with a pointer here). VPC flow logs are not enabled (see 0512).
- When to revisit: if a component must not have a public address, or for IPv6-only nodes.
