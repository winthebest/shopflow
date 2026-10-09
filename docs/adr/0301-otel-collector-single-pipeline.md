# 0301. One OpenTelemetry Collector pipeline; SLIs from span metrics

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-sre

## Context

Apps (Python/FastAPI) emit traces with the OTel SDK and JSON logs on stdout (`docs/contracts/services.md`).
We need traces in Tempo, logs in Loki, metrics in Prometheus, links between the three, and a request-based
SLI for checkout with a 300ms threshold. Promtail is EOL (2026-03); Grafana Alloy is an OTel Collector
distribution. Prometheus 3 has a native OTLP receiver. The SDK's default HTTP histogram buckets have no
300ms boundary, and changing them needs code in every service.

## Decision

- **OTel Collector (otelcol-k8s distribution, chart opentelemetry-collector) in two modes**:
  `otel-gateway` Deployment receives OTLP from apps; `otel-agent` DaemonSet tails `/var/log/pods` (filelog),
  parses the JSON log contract and attaches `trace_id`/`span_id`, then ships to Loki's native OTLP endpoint.
- **SLIs and RED metrics come from the `span_metrics` connector** in the gateway: every span becomes
  `traces_span_metrics_calls_total` and `traces_span_metrics_duration_seconds` (buckets we choose: 300ms for
  the SLO, 800ms/1s for the timeouts), with `http.route`, `http.response.status_code` and exemplars carrying
  `trace_id` (metric → trace in Grafana).
- **Metrics reach Prometheus through its native OTLP receiver** (`/api/v1/otlp`), with the translation
  strategy pinned (`UnderscoreEscapingWithSuffixes`) because SLO queries depend on the resulting names.
- Profile `obs-lite` keeps only the gateway (traces dropped after span metrics), so SLOs keep working
  without Loki/Tempo.

## Alternatives considered

| Option | Why not |
|---|---|
| Grafana Alloy | Same engine, but vendor-specific config language; the vendor-neutral Collector is the portfolio signal. Never run both |
| Promtail | EOL 2026-03 |
| SLI from app SDK metrics (`http.server.request.duration`) | No 300ms bucket without code changes in three services; SLI definition would live in sf-app code |
| Prometheus scraping `/metrics` of each app | Adds a second instrumentation path; no trace exemplars without extra work |
| Tempo metrics-generator | Remote-writes into Prometheus from inside Tempo; ties SLIs to the tracing backend, which `obs-lite` drops |
| Collector `prometheusremotewrite` exporter | Kept as fallback if the OTLP receiver misbehaves (Phase 3 risk) |

## Consequences

- Positive: one pipeline and one config language for all signals; the SLI threshold is a Collector setting,
  not app code; trace ↔ log ↔ metric links work from day one; configs are validated in CI with the same
  binary (`otelcol validate`).
- Negative / risks: SLIs depend on the Collector and on **100% trace sampling** (now in the services contract);
  any future sampling must sit after `span_metrics`. Gateway-side SLI is blind when all gateway pods are down
  (`CheckoutSLIMissing` guards this). The collector chart renamed components (`otlp` → `otlp_grpc`,
  `otlphttp` → `otlp_http`, `k8sattributes` → `k8s_attributes`, `filelog` → `file_log`); we use the new names.
- Measured (lane cluster, 2026-10-09): while Tempo was crash-looping, span metrics kept reporting the k6 rate
  (9.6–10.3 vs 10 checkouts/s). The exporter queue absorbed Tempo's failures, so the SLI was unaffected and only
  traces were lost.
- When to revisit: Collector CPU/RAM above budget in the Phase 3 measurements, or OTLP ingestion problems in
  Prometheus (→ remote write).
