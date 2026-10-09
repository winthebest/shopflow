# 0502. Layer 2 state holds only AWS resources

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

A session layer that also manages Kubernetes objects (Helm releases, `kubernetes_*` resources) needs a reachable
API server to plan or destroy. When the cluster is paused, its endpoint is restricted, or it is half-deleted,
`tofu destroy` then fails exactly when it is needed most, and the reaper would need cluster credentials.

## Decision

`infra/tofu/cluster` contains only AWS resources (EKS, launch template, node group, addons, Pod Identity
associations, access entries) and no kubernetes/helm provider. It finds layers 0 and 1 by name and tag (data
sources), not by reading their state. Argo CD and every workload are installed by `cloud-up` and GitOps.

## Alternatives considered

| Option | Why not |
|---|---|
| Helm/kubernetes providers in layer 2 | destroy depends on the API server; provider configuration from a resource being created is fragile |
| `terraform_remote_state` for layers 0/1 | the reaper role would need read access to every layer's state |

## Consequences

- Positive: `tofu destroy` works with AWS credentials only; the reaper role can read/lock/write a single state key.
- Negative: in-cluster cleanup (load balancers and volumes created by controllers) is done by scripts and the
  reaper's API teardown instead of OpenTofu.
- When to revisit: never for the reaper path; a separate in-cluster layer could be added if GitOps were dropped.
