# 0501. Ephemeral AWS sessions bounded by a lease and two reapers

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

The project has $100 of AWS credit for ~45 cloud hours. An EKS cluster costs money every hour it exists, and the
real risk is not the planned hours but a forgotten cluster. AWS Budgets refresh only every 8–12 hours, too slow to
be the main stop.

## Decision

Every compute session (layer 2) is created by `cloud-up` and destroyed by `cloud-down`, and carries a **lease** in
SSM (default 4 h, at most 8 h from now; `cloud-extend` moves it). A missing or unreadable lease counts as expired.
Two independent reapers enforce it with AWS APIs only (they work when the node group is paused or the EKS API is
closed): the GitHub workflow `cloud-reaper` (hourly, takes the layer-2 state lock before re-reading the lease) and a
Lambda in layer 0 scheduled by EventBridge Scheduler from lease + 1 h (does not depend on GitHub, which disables
cron after 60 days of inactivity). `cloud-up` writes a provisional lease before `tofu apply`, so a half-built
cluster is never "expired".

## Alternatives considered

| Option | Why not |
|---|---|
| Long-lived cluster, scaled to zero when idle | control plane ($0.10/h) and NLB keep billing; drift accumulates |
| Budgets / Budget Action as the only stop | delay of hours; credits hide spend unless excluded (0510) |
| One reaper only | GitHub cron can be disabled silently; a Lambda alone has no state lock or OpenTofu |

## Consequences

- Positive: worst case of a forgotten session is about lease + 1 h; every `cloud-up` is also a restore test.
- Negative: teardown order and resumability need care (finalizers, load balancers, volumes); covered by scripts
  that check state at every step and by an orphan check in every region.
- When to revisit: if session start-up time (RTO, measured by `cloud-up`) makes the demo workflow impractical.
