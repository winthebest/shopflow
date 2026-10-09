# 0202. Edge: Gateway API with Envoy Gateway, TLS from cert-manager

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

The shop needs one HTTPS entry point that behaves the same on k3d and on EKS (where an NLB fronts it). ingress-nginx
was retired in March 2026, Traefik is disabled in k3s to keep one edge, and the routing API should be the
Kubernetes standard rather than controller-specific annotations. Certificates must work offline on a laptop and
the same CA should later serve internal TLS (Trino HTTPS, Phase 4).

## Decision

- Gateway API CRDs (standard channel) + Envoy Gateway CRDs from `gateway-crds-helm` 1.9.1 in their own app at
  wave -2 (prune off: deleting a CRD deletes all its objects). Envoy Gateway 1.9.1 controller at wave -1.
- One `GatewayClass shopflow` → `EnvoyProxy shopflow` (Envoy image pinned by digest, `LoadBalancer` Service) and
  one `Gateway shopflow` in `envoy-gateway-system` with a single HTTPS listener. Only HTTPRoutes from namespace
  `shop` may attach; the hostname is set per overlay (`shop.127.0.0.1.sslip.io` locally).
- TLS: cert-manager 1.21.2 with Gateway API support. A self-signed root signs the `shopflow-ca` CA; the
  `shopflow-ca` ClusterIssuer issues `shop-tls` for the listener via the `cert-manager.io/cluster-issuer`
  annotation, so the Envoy Gateway app does not depend on cert-manager CRDs.

## Alternatives considered

| Option | Why not |
|---|---|
| ingress-nginx | Retired upstream (3/2026) |
| Traefik (k3s default) | Works locally, but its CRDs/annotations would differ from the EKS setup; one edge everywhere is simpler |
| Istio / Cilium Gateway | A mesh or CNI swap is far more than a single public route needs, and costs RAM on the laptop |
| Explicit `Certificate` in the gateway app | Couples the Envoy Gateway app to cert-manager CRDs in the same wave; the annotation keeps the apps independent |

## Consequences

- Positive: standard `HTTPRoute`s owned by the shop chart; the AWS overlay only changes the hostname and Service
  annotations for the NLB. The internal CA is reusable by any component through `issuerRef: shopflow-ca`.
- Negative / risks: the CA is self-signed, so clients use `curl -k` or trust `ca.crt` from Secret
  `cert-manager/shopflow-ca`. Envoy Gateway adds a controller + a proxy pod (~200MB locally). The Gateway listener
  hostname is fixed per environment; a second public host needs a second listener.
- When to revisit: a real domain (Let's Encrypt via the same cert-manager), or a need for TCP/TLS routes
  (switch the CRDs to the experimental channel).
