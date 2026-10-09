# Runbook: CheckoutSLIMissing

| | |
|---|---|
| Alert | `CheckoutSLIMissing` (`severity=ticket`), after 15 minutes |
| Rule | [`deploy/platform/slo/base/checkout-sli-guard.prometheusrule.yaml`](../../deploy/platform/slo/base/checkout-sli-guard.prometheusrule.yaml) |
| Why it exists | Without SLI data the burn-rate alerts stay silent: the SLO looks green while it is blind |

## What it means

Prometheus has no `traces_span_metrics_calls_total{service_name="gateway", span_kind="SPAN_KIND_SERVER",
http_route="/checkout"}` series. The OTel span_metrics connector keeps exporting every series it has seen, so
the series is missing only when:

1. no checkout request reached the gateway since the otel-gateway Collector (re)started (fresh cluster, idle
   lab), or
2. the pipeline app → otel-gateway → Prometheus is broken.

## Triage

1. Was there checkout traffic? On a fresh or idle lab cluster, send some checkouts through the gateway with
   `loadtest/checkout.js` (k6, e.g. `-e DURATION=1m`); the alert resolves once a checkout goes through. If it
   does, stop here. (Health probes `/healthz`, `/readyz` are excluded from tracing, so an idle gateway emits no
   spans at all.)
2. Collector up? `kubectl -n observability get pods -l app.kubernetes.io/instance=otel-gateway` and
   `kubectl -n observability logs deploy/otel-gateway | tail -50` (export errors to Prometheus or Tempo).
3. Apps exporting? Shop pods must have `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-gateway.observability.svc:4317`
   and must not have `OTEL_SDK_DISABLED=true` (`docs/contracts/services.md`).
   `kubectl -n shop get deploy gateway -o yaml | grep -A1 OTEL_`.
4. Prometheus ingesting OTLP? In Grafana Explore (Prometheus) run
   `count by (service_name) (traces_span_metrics_calls_total)`. Nothing at all → check the Prometheus logs
   (`kubectl -n observability logs prometheus-kps-prometheus-0 -c prometheus | grep -i otlp`).
5. Gateway spans present but no `/checkout` route? The gateway must emit stable HTTP semconv
   (`OTEL_SEMCONV_STABILITY_OPT_IN=http`), otherwise `http.route` / `http.response.status_code` are missing.

## Mitigation

Fix the broken hop (Collector config in `deploy/platform/otel-collector/`, app env in the shop chart). While
the alert fires, treat the checkout SLOs as **unknown**, not green.
