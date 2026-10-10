# 0303. Chaos Mesh, installed only for game days

- Status: Accepted
- Date: 2026-10-10
- Lane: sf-sre

## Context

Phase 7 needs fault injection inside Kubernetes (network delay, pod kill, pod failure) for at least five game
days. Chaos Mesh's daemon runs privileged on every node, and versions before 2.7.3 exposed an unauthenticated
GraphQL server that allowed cluster takeover (CVE-2025-59358..61). A permanently installed chaos tool is standing
risk, and it is also a temptation to "just try something" outside a planned game day.

## Decision

- **Chaos Mesh 2.8.4** (chart and images pinned by digest), as its own Argo CD app `chaos-mesh`, listed **only** in
  profile `chaos`. `make sre-chaos-on` adds it at the start of a game day. `make sre-chaos-off` removes it right
  after and fails unless no CRD, daemon or webhook is left. CI fails if any other profile lists it.
- Hardened: no dashboard, no DNS server, one controller replica. `enableFilterNamespace` limits targets to
  namespaces annotated `chaos-mesh.org/inject=enabled` (`shop`, `kafka`). NetworkPolicies open only the webhook
  port of the controller and the daemon's gRPC port to the controller. Webhook and daemon mTLS certificates come
  from cert-manager (the chart's in-template certificates would be regenerated on every Argo CD render).
- Experiments are plain YAML in `chaos/`, applied with `kubectl` during the game day (start/stop is the event,
  so it is not GitOps-synced). CI checks them against the CRDs of the pinned chart (strict), and checks that
  they target only allowed namespaces and stop by themselves unless one-shot.

## Alternatives considered

| Option | Why not |
|---|---|
| Litmus | Heavier (portal, MongoDB) for the same experiments |
| Chaos Mesh always installed | Privileged daemon and a webhook on every pod create, all the time; violates "only during game days" |
| Hand-rolled faults (`kubectl delete pod`, `tc` in a debug pod) | No duration/auto-stop, no audit trail, harder to repeat exactly |
| AWS FIS only | Covers node/AZ faults on EKS, not pod/network faults locally (kept Optional for spot interruption) |

## Consequences

- Positive: chaos exists only while someone is watching; experiments are reviewed, schema-checked and repeatable;
  the same files run locally and on EKS (only the containerd socket differs per overlay).
- Negative / risks: installing it is part of every game day (a few minutes). The daemon is still privileged while
  it runs, so Kyverno/PSA in Phase 8 need a narrow exception for `chaos-daemon` by name and digest.
  `sre-chaos-off` must run even if the game day is aborted.
- When to revisit: a Chaos Mesh CVE without a fixed release, or Kyverno rules that cannot express a narrow
  exception for the daemon.
