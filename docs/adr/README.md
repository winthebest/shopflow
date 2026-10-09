# Architecture Decision Records

One page per decision, using `0000-template.md`. File name: `NNNN-kebab-case-title.md`.

Parallel lanes draw numbers from their own range to avoid collisions. Lanes do not edit the index below; the
orchestrator adds rows when merging a PR that contains ADRs.

| Range | Lane |
|---|---|
| 0001–0099 | orchestrator |
| 0100–0199 | sf-app |
| 0200–0299 | sf-platform |
| 0300–0399 | sf-sre |
| 0400–0499 | sf-data |
| 0500–0599 | sf-cloud |

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0100](0100-app-language-python-fastapi.md) | Shop services in Python 3.12 + FastAPI (uv workspace) | Accepted |
| [0101](0101-data-contracts-in-ci.md) | Data contracts for CDC source tables, checked in CI against the migrated schema | Accepted |
| [0200](0200-k3d-over-kind.md) | Local Kubernetes on k3d (not kind or minikube) | Accepted |
| [0201](0201-argocd-over-flux.md) | GitOps with Argo CD app-of-apps (not Flux) | Accepted |
| [0202](0202-gateway-api-envoy-gateway.md) | Edge: Gateway API with Envoy Gateway, TLS from cert-manager | Accepted |
| [0204](0204-sops-ksops-local-secrets.md) | Local secrets with SOPS + age, decrypted by KSOPS in Argo CD | Accepted |
| [0205](0205-admin-ui-port-forward-only.md) | Admin UIs only through `kubectl port-forward` | Accepted |
| [0300](0300-slo-tooling-sloth.md) | Generate SLO rules with Sloth (Pyrra as fallback) | Accepted |
| [0301](0301-otel-collector-single-pipeline.md) | One OpenTelemetry Collector pipeline; SLIs from span metrics | Accepted |
| [0302](0302-observability-backends-chart-sources.md) | Observability backends, chart sources and pinning | Accepted |
