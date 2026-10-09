# 0512. Security baseline from the first cloud session

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

Hardening added after the first deployment tends to break running components and is postponed forever. On AWS the
cluster is internet-reachable and holds real credentials (Pod Identity, SSM), so the baseline must exist before the
first session, and local must match it so problems show up on k3d first.

## Decision

From the first session (and locally, by sf-platform): Pod Security Admission `restricted` for app namespaces;
default-deny NetworkPolicy with per-namespace allow-lists (including egress to S3/Glue/SSM/STS and the Pod Identity
agent `169.254.170.23`, enforced by vpc-cni network policy on AWS). On the AWS side (this lane): IMDSv2 with hop
limit 1, Pod Identity roles pinned to one service account and one data prefix, EKS API and NLB limited to the
operator's IP, access entries instead of `aws-auth`, EKS audit and authenticator logs kept 7 days in a log group
that outlives the cluster, permissions boundary on every role, no long-lived keys, `trivy config` in CI with every
ignored finding justified next to the code.

Not enabled, by cost: VPC flow logs, customer KMS keys (SSE-S3 / AWS-owned keys instead), EKS api/scheduler/
controllerManager logs, Lambda X-Ray.

## Alternatives considered

| Option | Why not |
|---|---|
| Harden after the platform works | baseline changes then break running workloads and get deferred |
| Everything on (flow logs, KMS, all control plane logs) | recurring cost with little value for short single-operator sessions |

## Consequences

- Positive: security tests in Phase 6 (other IP cannot reach NLB/API, ci-plan cannot read SSM) have something to test.
- Negative: IMDS hop limit 1 requires explicit region/VPC configuration for controllers; more NetworkPolicy work.
- When to revisit: if a compliance-style demo needs flow logs or customer-managed keys.
