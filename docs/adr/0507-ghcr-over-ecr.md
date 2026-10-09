# 0507. Images stay in GHCR; no ECR

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

CI already builds multi-arch images and pushes them to `ghcr.io/winthebest/shopflow-*`; manifests pin them by
digest (signing arrives in Phase 8). ECR would add a registry per region, a mirroring job and IAM for pulls.

## Decision

EKS nodes pull directly from GHCR over the internet gateway (0503). No ECR repositories, no pull-through cache.
The node role keeps `AmazonEC2ContainerRegistryPullOnly` only for EKS-managed addon images.

## Alternatives considered

| Option | Why not |
|---|---|
| ECR + mirror job | second copy of every image, storage cost, more IAM, no benefit for public images |
| ECR pull-through cache for GHCR | needs a GitHub token in Secrets Manager ($0.40/month) and still copies every image |

## Consequences

- Positive: one registry and one digest/signing story for local and AWS.
- Negative: pulls depend on GHCR availability and its rate limits; private packages would need an image pull
  secret (from SSM through External Secrets).
- When to revisit: if pulls are throttled or the packages become private.
