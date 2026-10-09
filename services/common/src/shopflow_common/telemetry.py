"""OpenTelemetry tracing setup, driven by the standard OTEL_* environment variables (docs/contracts/services.md).

- `OTEL_SDK_DISABLED=true` (the default in images until Phase 3): nothing is instrumented, zero overhead.
- `OTEL_TRACES_EXPORTER=none`: spans and trace ids exist (so logs carry `trace_id`) but nothing is exported.
- Otherwise spans go to OTLP/gRPC at `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4317`).
- `OTEL_SEMCONV_STABILITY_OPT_IN=http` (set in the images) makes server spans carry `http.route` (the route
  template, e.g. `/orders/{order_id}`) and `http.response.status_code` for the spanmetrics SLIs.
- No sampler is configured: the SDK default `parentbased_always_on` keeps 100% of traces, as the SLIs require.
"""

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def sdk_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


def setup_telemetry(app: FastAPI, service: str) -> TracerProvider | None:
    """Install the global tracer provider and instrument FastAPI + httpx. Returns None when disabled."""
    if sdk_disabled():
        return None

    # OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES win over the built-in service name when set.
    resource = Resource.create({SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME", service)})
    provider = TracerProvider(resource=resource)
    if os.environ.get("OTEL_TRACES_EXPORTER", "otlp").strip().lower() != "none":
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
    HTTPXClientInstrumentor().instrument()
    return provider
