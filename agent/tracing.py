"""The agent's telemetry: OpenTelemetry GenAI spans and the metrics of agent/instruments.py, without content.

This module is the one place that names telemetry (docs/adr/0106-genai-tracing.md). Attribute names are the GenAI
semantic conventions as published in opentelemetry-semantic-conventions 0.66b0 (since moved, unchanged, to the
semantic-conventions-genai repository, which has no release); a test checks them against that package. Token counts
are what the provider reports: for Claude, `gen_ai.usage.input_tokens` excludes cache reads and writes, which have
their own attributes.

Content never leaves the process. Spans get only the attributes in ALLOWED_ATTRIBUTES: no prompt, completion, tool
argument or result, SQL, table name or error message is ever set; an error is its exception class (`error.type`).
`ContentFreeExporter` enforces the same allowlist at export and drops events and status descriptions, so a mistake
upstream cannot leak content either.

    telemetry = Telemetry(mode="eval", arm="guarded", prompt_version=..., policy_version=...)
    telemetry.prime(models=[...], action_classes=[...])   # counters start at 0 for every label set
    with telemetry.task(model) as task:
        turn = conversation.send_user(...)
        telemetry.turn(provider.name, turn, task)
        with telemetry.tool(call):
            ...
        task.outcome = "executed"
"""

import os
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from agent import instruments
from agent.llm.base import ToolCall, Turn

LAB_COLLECTOR = "http://127.0.0.1:24318"  # lab/compose.yml OTLP/HTTP (docs/contracts/environment.md)

# GenAI semantic conventions (opentelemetry-semantic-conventions 0.66b0)
OPERATION_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
FINISH_REASONS = "gen_ai.response.finish_reasons"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
AGENT_NAME = "gen_ai.agent.name"
TOKEN_TYPE = "gen_ai.token.type"  # noqa: S105 - an attribute name, not a secret
ERROR_TYPE = "error.type"
# The agent's own
MODE = "agent.mode"
ARM = "agent.arm"
OUTCOME = "agent.outcome"
ACTION_CLASS = "agent.action_class"
PROMPT_VERSION = "agent.prompt.version"
POLICY_VERSION = "agent.policy.version"
COST = "agent.cost.usd"
FALLBACK = "agent.fallback"
MALFORMED_TOOL_CALLS = "agent.tool_calls.malformed"
CONTEXT_LIMIT_HIT = "agent.context_limit_hit"

ALLOWED_ATTRIBUTES = frozenset(
    {
        OPERATION_NAME,
        PROVIDER_NAME,
        REQUEST_MODEL,
        RESPONSE_MODEL,
        FINISH_REASONS,
        INPUT_TOKENS,
        OUTPUT_TOKENS,
        CACHE_READ_TOKENS,
        CACHE_CREATION_TOKENS,
        TOOL_NAME,
        TOOL_CALL_ID,
        AGENT_NAME,
        ERROR_TYPE,
        MODE,
        ARM,
        OUTCOME,
        PROMPT_VERSION,
        POLICY_VERSION,
        COST,
        FALLBACK,
        MALFORMED_TOOL_CALLS,
        CONTEXT_LIMIT_HIT,
    }
)
OUTCOMES = ("executed", "approved", "blocked", "clarified", "failed", "invalid")


class ContentFreeExporter(SpanExporter):
    """Exports only allowlisted attributes, no events, and no status description, whatever the span carried."""

    def __init__(self, exporter: SpanExporter) -> None:
        self.exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.exporter.export([_content_free(span) for span in spans])

    def shutdown(self) -> None:
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.exporter.force_flush(timeout_millis)


def _content_free(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes={k: v for k, v in (span.attributes or {}).items() if k in ALLOWED_ATTRIBUTES},
        events=(),
        links=span.links,
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


@dataclass
class TaskRecord:
    model: str
    outcome: str = "failed"  # set by the caller before the task ends
    cost_usd: float | None = 0.0  # None once a turn had no list price


class Telemetry:
    def __init__(
        self,
        *,
        mode: str,
        arm: str,
        prompt_version: str,
        policy_version: str,
        agent_name: str = "lakehouse-operator",
        tracer_provider: trace.TracerProvider | None = None,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.common = {MODE: mode, ARM: arm}
        self.versions = {PROMPT_VERSION: prompt_version, POLICY_VERSION: policy_version}
        self.tracer = trace.get_tracer("agent", tracer_provider=tracer_provider)
        meter = metrics.get_meter("agent", meter_provider=meter_provider)
        self.instruments = {
            i.name: (
                meter.create_counter(i.name, unit=i.unit, description=i.description)
                if i.kind == "counter"
                else meter.create_histogram(
                    i.name, unit=i.unit, description=i.description, explicit_bucket_boundaries_advisory=i.buckets
                )
            )
            for i in instruments.ALL
        }

    def _add(self, instrument: instruments.Instrument, value: float, **attributes: object) -> None:
        self.instruments[instrument.name].add(value, {**self.common, **attributes})

    def _record(self, instrument: instruments.Instrument, value: float, **attributes: object) -> None:
        self.instruments[instrument.name].record(value, {**self.common, **attributes})

    def prime(self, models: Iterable[str], action_classes: Iterable[str]) -> None:
        """Every counter at 0 for every label set this process can emit, so the first increase shows."""
        models, action_classes = list(models), list(action_classes)
        for action_class in action_classes:
            self._add(instruments.UNSAFE_ACTIONS, 0, **{ACTION_CLASS: action_class})
        for model in models:
            for outcome in OUTCOMES:
                self._add(instruments.TASKS, 0, **{OUTCOME: outcome, REQUEST_MODEL: model})
            for counter in (instruments.MALFORMED_TOOL_CALLS, instruments.CONTEXT_LIMIT_HITS, instruments.FALLBACKS):
                self._add(counter, 0, **{REQUEST_MODEL: model})

    @contextmanager
    def task(self, model: str) -> Iterator[TaskRecord]:
        record = TaskRecord(model)
        started = time.monotonic()
        attributes = {OPERATION_NAME: "invoke_agent", AGENT_NAME: self.agent_name, REQUEST_MODEL: model}
        with self.tracer.start_as_current_span(
            f"invoke_agent {self.agent_name}", attributes={**attributes, **self.common, **self.versions}
        ) as span:
            try:
                yield record
            except Exception as exc:
                record.outcome = "failed"
                span.set_attribute(ERROR_TYPE, type(exc).__qualname__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                span.set_attribute(OUTCOME, record.outcome)
                if record.cost_usd is not None:
                    span.set_attribute(COST, record.cost_usd)
                labels = {OUTCOME: record.outcome, REQUEST_MODEL: model}
                self._add(instruments.TASKS, 1, **labels)
                self._record(instruments.TASK_DURATION, time.monotonic() - started, **labels)
                if record.cost_usd is not None:
                    self._record(instruments.TASK_COST, record.cost_usd, **{REQUEST_MODEL: model})

    def turn(self, provider: str, turn: Turn, task: TaskRecord | None = None) -> None:
        """One LLM call, as a `chat` span ending now and lasting the turn's duration, plus its metrics."""
        end = time.time_ns()
        usage = turn.usage
        attributes = {
            OPERATION_NAME: "chat",
            PROVIDER_NAME: provider,
            REQUEST_MODEL: turn.requested_model,
            RESPONSE_MODEL: turn.served_model,
            FINISH_REASONS: (turn.stop.value,),
            INPUT_TOKENS: usage.input_tokens,
            OUTPUT_TOKENS: usage.output_tokens,
            CACHE_READ_TOKENS: usage.cache_read_input_tokens,
            CACHE_CREATION_TOKENS: usage.cache_creation_input_tokens,
            FALLBACK: turn.fallback,
            MALFORMED_TOOL_CALLS: turn.malformed_tool_calls,
            CONTEXT_LIMIT_HIT: turn.context_limit_hit,
            **self.common,
        }
        if turn.cost_usd is not None:
            attributes[COST] = turn.cost_usd
        span = self.tracer.start_span(
            f"chat {turn.requested_model}", start_time=end - int(turn.duration_s * 1e9), attributes=attributes
        )
        span.end(end_time=end)

        models = {PROVIDER_NAME: provider, REQUEST_MODEL: turn.requested_model, RESPONSE_MODEL: turn.served_model}
        self._record(instruments.TOKEN_USAGE, usage.input_tokens, **models, **{TOKEN_TYPE: "input"})
        self._record(instruments.TOKEN_USAGE, usage.output_tokens, **models, **{TOKEN_TYPE: "output"})
        self._record(instruments.OPERATION_DURATION, turn.duration_s, **models)
        if turn.malformed_tool_calls:
            self._add(
                instruments.MALFORMED_TOOL_CALLS, turn.malformed_tool_calls, **{REQUEST_MODEL: turn.requested_model}
            )
        if turn.context_limit_hit:
            self._add(instruments.CONTEXT_LIMIT_HITS, 1, **{REQUEST_MODEL: turn.requested_model})
        if turn.fallback:
            self._add(instruments.FALLBACKS, 1, **{REQUEST_MODEL: turn.requested_model})
        if task is not None:
            task.cost_usd = None if task.cost_usd is None or turn.cost_usd is None else task.cost_usd + turn.cost_usd

    @contextmanager
    def tool(self, call: ToolCall) -> Iterator[None]:
        attributes = {OPERATION_NAME: "execute_tool", TOOL_NAME: call.name, TOOL_CALL_ID: call.id, **self.common}
        with self.tracer.start_as_current_span(f"execute_tool {call.name}", attributes=attributes) as span:
            try:
                yield
            except Exception as exc:
                span.set_attribute(ERROR_TYPE, type(exc).__qualname__)
                span.set_status(Status(StatusCode.ERROR))
                raise

    def unsafe_action(self, action_class: str) -> None:
        self._add(instruments.UNSAFE_ACTIONS, 1, **{ACTION_CLASS: action_class})


def configure(service_name: str = "operator-agent", endpoint: str | None = None) -> tuple[object, object]:
    """Tracer and meter providers that export OTLP/HTTP (by default to the lab Collector), spans content-free."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", LAB_COLLECTOR)
    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = ContentFreeExporter(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"))
    return tracer_provider, MeterProvider(resource=resource, metric_readers=[reader])
