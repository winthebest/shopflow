"""End to end: one traced task exported to the lab Collector arrives there without content (docs/adr/0106).

Needs the lab stack (`lab/stack.sh up`): the Collector listens on 127.0.0.1:24318 and writes what it receives to
`$LAB_RUN_DIR/otel/{traces,metrics}.jsonl` (LAB_RUN_DIR: the stack's run directory, absolute, by default `out/lab`
of the checkout that started it). Only those two files are read: the run directory also holds the stack's
credentials. The task runs against the fake Ollama backend, so no model is loaded. Records of this run are found by a
unique `service.instance.id`.

    LAB_RUN_DIR=<stack's run dir> uv run pytest -m lab -v
"""

import json
import os
import socket
import time
import uuid
from pathlib import Path

import fake_ollama as fo
import httpx2
import pytest
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from agent import instruments, tracing
from agent.llm.base import ToolResult, ToolSpec
from agent.llm.ollama_provider import OllamaProvider

MARKER = "lab_ro.shop.orders_e2e_9d1c"
OTEL_DIR = Path(os.environ.get("LAB_RUN_DIR", Path(__file__).resolve().parents[2] / "out" / "lab")) / "otel"
TOOL = ToolSpec(
    "row_count",
    "Number of rows in a table",
    {
        "type": "object",
        "properties": {"table": {"type": "string"}},
        "required": ["table"],
        "additionalProperties": False,
    },
)


def collector_problem() -> str | None:
    try:
        socket.create_connection(("127.0.0.1", 24318), timeout=1).close()
    except OSError:
        return "no lab Collector on 127.0.0.1:24318 (lab/stack.sh up)"
    if not OTEL_DIR.is_dir():
        return f"{OTEL_DIR} not found: set LAB_RUN_DIR to the stack's run directory"
    return None


PROBLEM = collector_problem()
pytestmark = [pytest.mark.lab, pytest.mark.skipif(PROBLEM is not None, reason=f"lab Collector: {PROBLEM}")]


def lines_of(path: Path, instance: str) -> list[str]:
    """Lines written for this run (the file holds every run's records)."""
    deadline = time.monotonic() + 20  # the Collector batches and flushes its files within about 2 s
    while time.monotonic() < deadline:
        found = [line for line in path.read_text().splitlines() if instance in line] if path.exists() else []
        if found:
            return found
        time.sleep(0.5)
    return []


def test_a_traced_task_reaches_the_lab_collector_without_content():
    instance = str(uuid.uuid4())
    resource = Resource.create({"service.name": "operator-agent-e2e", "service.instance.id": instance})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(
            tracing.ContentFreeExporter(OTLPSpanExporter(endpoint=f"{tracing.LAB_COLLECTOR}/v1/traces"))
        )
    )
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{tracing.LAB_COLLECTOR}/v1/metrics"))
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    telemetry = tracing.Telemetry(
        mode="eval",
        arm="guarded",
        prompt_version="e2e",
        policy_version="e2e",
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    telemetry.prime(models=["qwen3:8b"], action_classes=["delete"])
    backend = fo.FakeOllama(fo.Reply(tool_calls=[fo.call("row_count", {"table": MARKER})]), fo.Reply(content="1234."))
    provider = OllamaProvider(http=httpx2.Client(base_url="http://ollama.test", transport=backend.transport))
    with telemetry.task(provider.model) as task:
        conversation = provider.start("You operate a data lakehouse.", [TOOL])
        turn = conversation.send_user(f"How many rows does {MARKER} have?")
        telemetry.turn(provider.name, turn, task)
        with telemetry.tool(turn.tool_calls[0]):
            result = f"{MARKER}: 1234"
        telemetry.turn(provider.name, conversation.send_tool_results([ToolResult(turn.tool_calls[0], result)]), task)
        task.outcome = "executed"
    tracer_provider.shutdown()
    meter_provider.shutdown()

    traces, metrics = lines_of(OTEL_DIR / "traces.jsonl", instance), lines_of(OTEL_DIR / "metrics.jsonl", instance)
    assert traces and metrics, "nothing from this run reached the Collector's files"
    names = {
        span["name"]
        for line in traces
        for rs in json.loads(line)["resourceSpans"]
        for ss in rs["scopeSpans"]
        for span in ss["spans"]
    }
    assert names == {"invoke_agent lakehouse-operator", "chat qwen3:8b", "execute_tool row_count"}
    metric_names = {
        m["name"]
        for line in metrics
        for rm in json.loads(line)["resourceMetrics"]
        for sm in rm["scopeMetrics"]
        for m in sm["metrics"]
    }
    assert {instruments.TASKS.name, instruments.TOKEN_USAGE.name, instruments.UNSAFE_ACTIONS.name} <= metric_names
    assert not [line for line in traces + metrics if MARKER in line], "content reached the Collector"
