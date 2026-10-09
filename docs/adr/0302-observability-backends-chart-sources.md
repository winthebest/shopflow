# 0302. Observability backends, chart sources and pinning

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-sre

## Context

Profile `obs` must fit in ~3GB on a 24GB laptop (Docker VM 16GB), use only maintained OSS, no Bitnami images,
pinned chart versions and image digests. On 2026-10-09 we checked the chart repositories:

- `grafana/helm-charts`: the `grafana` and `tempo` charts were deprecated on 2026-01-30 and migrated; the
  `loki` chart README says the OSS chart moved on 2026-03-16 and the old one is now for Grafana Enterprise Logs
  only (grafana/loki#20705).
- `grafana-community/helm-charts` (HTTP and OCI on ghcr.io) now publishes `loki`, `tempo`, `grafana`, with
  commits daily; kube-prometheus-stack 92.2.0 already pulls its Grafana subchart from there.

## Decision

| Component | Chart (pinned) | App | Mode |
|---|---|---|---|
| kube-prometheus-stack | prometheus-community 92.2.0 | Prometheus v3.15.0, Alertmanager v0.34.1, operator v0.94.1, Grafana 13.2.3 | Prometheus with OTLP receiver + exemplar storage |
| Loki | grafana-community 18.15.1 | 3.7.8 | Monolithic, filesystem, TSDB v13, OTLP ingest |
| Tempo | grafana-community 3.1.0 | 3.1.0 | Single binary, local backend |
| OTel Collector | open-telemetry 0.175.1 | otelcol-k8s 0.161.0 | gateway + agent (ADR 0301) |

- Every image is pinned by multi-arch index digest in values; CI fails on any rendered image without
  `@sha256:` (`make sre-lint`).
- Installed by Argo CD multi-source Applications (`docs/contracts/gitops.md`); chart version, release name and
  value files live only in `deploy/argocd/apps/<component>/application.yaml`, and CI renders from that file.
- Local retention: Prometheus 3d/4GB, Loki 48h, Tempo 48h; PVCs 5Gi each.
- Grafana admin from Secret `grafana-admin`; Alertmanager reads the Discord webhook from a mounted Secret
  file (`webhook_url_file`), so the URL is never in values or in the rendered config. No UI is exposed
  (port-forward only).
- prometheus-operator admission webhooks are disabled: rules are validated in CI with `promtool`, and the
  webhook's runtime-patched `caBundle` would keep Argo CD out of sync.
- Loki and Tempo Grafana datasources ship with their own components (sidecar ConfigMaps), so `obs-lite`
  has no dangling datasources.

## Alternatives considered

| Option | Why not |
|---|---|
| `grafana/loki`, `grafana/tempo` charts | Deprecated/migrated for OSS users in 2026 |
| Loki SimpleScalable / distributed | Requires object storage and 3+ pods; deprecated mode in Loki 4 |
| Mimir or Thanos for long-term metrics | RAM; 3-day local retention is enough for a lab with soak tests |
| Grafana Operator for dashboards/datasources | Extra controller; the chart sidecar does the job |
| Bitnami charts (e.g. for Grafana) | Bitnami images moved to legacy/paid |

## Consequences

- Positive: maintained chart sources; reproducible renders; `obs-lite` (~1.5GB target) is a strict subset.
- Negative / risks: Grafana-community charts are community-maintained (renames like `SingleBinary` →
  `Monolithic` already happened); the Tempo chart has no digest field, so the digest rides in the tag
  (`3.1.0@sha256:…`). Digests must be bumped by hand until Renovate (Optional) exists.
- When to revisit: Phase 3 RAM measurements above the profile budget, or a chart source moving again.
