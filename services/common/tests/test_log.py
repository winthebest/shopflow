import json
import logging
import os
import re
import subprocess
import sys
import textwrap

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from shopflow_common.log import AccessLogMiddleware, configure_logging


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level


def last_json_line(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


@pytest.mark.usefixtures("restore_root_logger")
def test_log_line_has_contract_keys_and_empty_trace_outside_span(capsys):
    configure_logging("svc", "INFO")
    logging.getLogger("x").info("hello", extra={"order_id": 7})

    line = last_json_line(capsys.readouterr().out)
    assert {"timestamp", "level", "message", "service", "trace_id", "span_id"} <= line.keys()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", line["timestamp"])
    assert line["level"] == "INFO"
    assert line["message"] == "hello"
    assert line["service"] == "svc"
    assert line["order_id"] == 7
    assert line["trace_id"] == ""
    assert line["span_id"] == ""


@pytest.mark.usefixtures("restore_root_logger")
def test_critical_is_reported_as_error(capsys):
    configure_logging("svc", "INFO")
    logging.getLogger("x").critical("down")
    assert last_json_line(capsys.readouterr().out)["level"] == "ERROR"


@pytest.mark.usefixtures("restore_root_logger")
def test_log_line_carries_current_span_ids(capsys):
    configure_logging("svc", "INFO")
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("op") as span:
        logging.getLogger("x").info("inside")

    ctx = span.get_span_context()
    line = last_json_line(capsys.readouterr().out)
    assert line["trace_id"] == format(ctx.trace_id, "032x")
    assert line["span_id"] == format(ctx.span_id, "016x")


def run_instrumented_app(env: dict[str, str]) -> tuple[dict, list[dict]]:
    """setup_telemetry() installs global providers, so run it in a fresh interpreter.

    Returns (summary, log lines); the summary holds whether a provider was installed, the meter provider's type and
    the finished spans.
    """
    script = textwrap.dedent(
        """
        import json
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from opentelemetry import metrics
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from shopflow_common.log import AccessLogMiddleware, configure_logging
        from shopflow_common.telemetry import setup_telemetry

        configure_logging("svc")
        app = FastAPI()
        app.add_middleware(AccessLogMiddleware)

        @app.get("/items/{item_id}")
        def item(item_id: int):
            return {"id": item_id}

        provider = setup_telemetry(app, "svc")
        exporter = InMemorySpanExporter()
        if provider is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        with TestClient(app) as client:
            client.get("/items/42")
        spans = [{"kind": s.kind.name, **s.attributes} for s in exporter.get_finished_spans()]
        meter = type(metrics.get_meter_provider()).__name__
        print(json.dumps({"summary": True, "provider": provider is not None, "meter": meter, "spans": spans}))
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", script],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    lines = [json.loads(line) for line in result.stdout.strip().splitlines()]
    return lines[-1], lines[:-1]


def test_sdk_enabled_gives_trace_ids_in_logs_and_route_templates_on_spans():
    summary, logs = run_instrumented_app(
        {
            "OTEL_SDK_DISABLED": "false",
            "OTEL_TRACES_EXPORTER": "none",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "http",
        }
    )
    access = next(line for line in logs if line["logger"] == "access")
    server = next(span for span in summary["spans"] if span["kind"] == "SERVER")

    assert summary["provider"] is True
    assert summary["meter"] == "MeterProvider"  # the SDK one: metrics such as orders' circuit state are recorded
    assert access["http_path"] == "/items/42"
    assert access["http_status"] == 200
    assert len(access["trace_id"]) == 32
    assert len(access["span_id"]) == 16
    assert server["http.route"] == "/items/{item_id}"  # template, not the real path
    assert server["http.response.status_code"] == 200


def test_sdk_disabled_installs_nothing():
    summary, logs = run_instrumented_app({"OTEL_SDK_DISABLED": "true"})
    access = next(line for line in logs if line["logger"] == "access")
    assert summary["provider"] is False
    assert summary["meter"] != "MeterProvider"  # API default: instruments are no-ops
    assert access["trace_id"] == ""
    assert access["span_id"] == ""


@pytest.mark.usefixtures("restore_root_logger")
async def test_unhandled_error_is_logged_inside_the_span(capsys):
    configure_logging("svc", "INFO")
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    tracer = TracerProvider().get_tracer("test")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with tracer.start_as_current_span("request") as span:
        async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
            response = await client.get("/boom")

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    error = next(line for line in lines if line["message"] == "unhandled error")
    access = next(line for line in lines if line["message"] == "request")
    assert response.status_code == 500
    assert error["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert "RuntimeError: boom" in error["exception"]
    assert access["level"] == "ERROR"
    assert access["http_status"] == 500


@pytest.mark.usefixtures("restore_root_logger")
def test_extra_fields_cannot_overwrite_contract_keys(capsys):
    configure_logging("svc", "INFO")
    logging.getLogger("x").info("hello", extra={"service": "spoofed", "trace_id": "nope", "color_message": "x"})
    line = last_json_line(capsys.readouterr().out)
    assert line["service"] == "svc"
    assert line["trace_id"] == ""
    assert "color_message" not in line
