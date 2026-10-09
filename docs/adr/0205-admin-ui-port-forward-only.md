# 0205. Admin UIs only through `kubectl port-forward`

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

The platform has several admin UIs: Argo CD now, then Grafana, Airflow, Metabase and Trino. On EKS the Gateway is
reachable from the internet through an NLB; the red-team review flagged admin UIs exposed with default passwords as
a high-severity risk. Locally the same manifests are used, so whatever is exposed locally would be exposed on AWS.

## Decision

- Only the shop gets an `HTTPRoute`. The Gateway accepts routes only from namespace `shop` (`allowedRoutes`
  selector), so another namespace cannot publish a route even by mistake.
- Admin UIs keep `ClusterIP` Services and are reached with `kubectl port-forward` bound to `127.0.0.1`
  (`make platform-argocd-ui`: `sf-main` 18080, lane clusters 18081–18084), the same on k3d and EKS.
- Every admin password comes from SOPS (ADR 0204), never from a chart default.

## Alternatives considered

| Option | Why not |
|---|---|
| Routes for admin UIs behind basic auth / OIDC | Adds an auth proxy or IdP to run and secure; one weak password exposes cluster control |
| Separate internal Gateway | On EKS it still needs a private LB plus VPN/bastion to reach it; port-forward gives the same reach with the existing API auth |
| NodePort / LoadBalancer Services for UIs | Bypasses the Gateway policy entirely |

## Consequences

- Positive: the public attack surface is one route; access to admin UIs requires cluster credentials (and on EKS
  an operator IP allowed by the API endpoint).
- Negative / risks: port-forward is single-user and drops with the API connection; links in alerts must point to
  `localhost` ports. Webhooks into Argo CD cannot reach it, so Git is polled (60s).
- When to revisit: several people need the UIs at once, or a demo requires a shareable URL (then add an internal
  Gateway with OIDC, recorded as a new ADR).
