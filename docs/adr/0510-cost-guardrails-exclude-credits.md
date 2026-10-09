# 0510. Cost guardrails that exclude credits

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

AWS applies promotional credits before Budgets evaluate spend. A default budget therefore shows $0 and never alerts
while the credit lasts, which is exactly the period of this project. The project runs October 2026 to about March
2027, crossing a year boundary, so a monthly or annual budget would reset its thresholds mid-project.

## Decision

One cost budget `shopflow-total` with `cost_types { include_credit = false, include_refund = false }` and a
**custom period 2026-10-01 → 2027-10-01** (one cumulative window, no reset). Emails at $10 and $20 actual and $25
forecast. A **Budget Action at $25 actual** attaches a Deny policy (no new EKS cluster/node group, EC2, EBS, ELB,
NAT, EIP; teardown still works) to the operator and the LB controller roles. Cost Anomaly Detection sends each
anomaly ≥ $1 to SNS immediately (reusing AWS's default services monitor when present). `project`/`env` are cost
allocation tags, propagated by launch template tag specifications, EBS CSI and vpc-cni extra tags and the LB
controller default tags.

## Alternatives considered

| Option | Why not |
|---|---|
| Budget including credits (default) | never alerts during the credit period |
| Monthly or annual budget | thresholds reset on the 1st of the month / of the period, mid-project |
| No Budget Action | relies on reading email in time |

## Consequences

- Positive: spend is visible from the first dollar; $25 is a deliberate checkpoint, not a limit by accident.
- Negative: Budgets lag 8–12 hours, so the lease and reapers (0501) remain the primary stop; the API marks
  `CostTypes` as deprecated (move to a `filter_expression` on record type if it is removed).
- When to revisit: after reviewing `docs/cost.md` at the $25 checkpoint, or when the credit expires.
