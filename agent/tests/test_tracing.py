"""agent/tracing.py: GenAI spans and agent metrics with no content in them, on a real provider turn (fake backend)."""

import json

import fake_ollama as fo
import httpx2
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as semconv
from opentelemetry.trace import Link, Status, StatusCode

from agent import instruments, tracing
from agent.llm.base import ToolCall, ToolResult, ToolSpec
from agent.llm.ollama_provider import OllamaProvider

MARKER = "lab_ro.shop.orders_7f3a"  # in the prompt, the tool input and the tool result: must not reach telemetry
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


@pytest.fixture
def telemetry():
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(tracing.ContentFreeExporter(spans)))
    reader = InMemoryMetricReader()
    telemetry = tracing.Telemetry(
        mode="eval",
        arm="guarded",
        prompt_version="p-1a2b",
        policy_version="g-3c4d",
        tracer_provider=tracer_provider,
        meter_provider=MeterProvider(metric_readers=[reader]),
    )
    return telemetry, spans, reader


def run_task(telemetry: tracing.Telemetry) -> None:
    backend = fo.FakeOllama(
        fo.Reply(tool_calls=[fo.call("row_count", {"table": MARKER})]), fo.Reply(content="1234 rows.")
    )
    provider = OllamaProvider(http=httpx2.Client(base_url="http://ollama.test", transport=backend.transport))
    with telemetry.task(provider.model) as task:
        conversation = provider.start("You operate a data lakehouse.", [TOOL])
        turn = conversation.send_user(f"How many rows does {MARKER} have?")
        telemetry.turn(provider.name, turn, task)
        call = turn.tool_calls[0]
        with telemetry.tool(call):
            result = f"{MARKER}: 1234"
        telemetry.turn(provider.name, conversation.send_tool_results([ToolResult(call, result)]), task)
        task.outcome = "executed"


def points(reader: InMemoryMetricReader) -> dict[str, list]:
    found = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found[metric.name] = list(metric.data.data_points)
    return found


def test_attribute_names_are_the_pinned_semantic_conventions():
    assert tracing.OPERATION_NAME == semconv.GEN_AI_OPERATION_NAME
    assert tracing.PROVIDER_NAME == semconv.GEN_AI_PROVIDER_NAME
    assert tracing.REQUEST_MODEL == semconv.GEN_AI_REQUEST_MODEL
    assert tracing.RESPONSE_MODEL == semconv.GEN_AI_RESPONSE_MODEL
    assert tracing.FINISH_REASONS == semconv.GEN_AI_RESPONSE_FINISH_REASONS
    assert tracing.INPUT_TOKENS == semconv.GEN_AI_USAGE_INPUT_TOKENS
    assert tracing.OUTPUT_TOKENS == semconv.GEN_AI_USAGE_OUTPUT_TOKENS
    assert tracing.CACHE_READ_TOKENS == semconv.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS
    assert tracing.CACHE_CREATION_TOKENS == semconv.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS
    assert tracing.TOOL_NAME == semconv.GEN_AI_TOOL_NAME
    assert tracing.TOOL_CALL_ID == semconv.GEN_AI_TOOL_CALL_ID
    assert tracing.AGENT_NAME == semconv.GEN_AI_AGENT_NAME
    assert tracing.TOKEN_TYPE == semconv.GEN_AI_TOKEN_TYPE


def test_a_task_is_one_span_tree_with_versions_models_and_tokens(telemetry):
    telemetry, spans, _ = telemetry
    run_task(telemetry)

    finished = {span.name: span for span in spans.get_finished_spans()}
    assert set(finished) == {"invoke_agent lakehouse-operator", "chat qwen3:8b", "execute_tool row_count"}
    task = finished["invoke_agent lakehouse-operator"]
    assert all(span.parent.span_id == task.context.span_id for name, span in finished.items() if span is not task)
    assert task.attributes[tracing.PROMPT_VERSION] == "p-1a2b"
    assert task.attributes[tracing.POLICY_VERSION] == "g-3c4d"
    assert task.attributes[tracing.OUTCOME] == "executed"
    assert task.attributes[tracing.COST] == 0.0
    chat = [span for span in spans.get_finished_spans() if span.name == "chat qwen3:8b"]
    assert len(chat) == 2
    assert chat[0].attributes[tracing.PROVIDER_NAME] == "ollama"
    assert chat[0].attributes[tracing.RESPONSE_MODEL] == "qwen3:8b"
    assert chat[0].attributes[tracing.FINISH_REASONS] == ("tool_use",)
    assert chat[0].attributes[tracing.INPUT_TOKENS] > 0
    assert finished["execute_tool row_count"].attributes[tracing.TOOL_CALL_ID] == "call_1"


def test_no_content_reaches_the_spans(telemetry):
    telemetry, spans, _ = telemetry
    run_task(telemetry)
    dump = json.dumps(
        [[s.name, dict(s.attributes), [dict(e.attributes) for e in s.events]] for s in spans.get_finished_spans()]
    )
    assert MARKER not in dump
    assert all(key in tracing.ALLOWED_ATTRIBUTES for s in spans.get_finished_spans() for key in s.attributes)


def test_the_exporter_drops_content_set_by_mistake(telemetry):
    telemetry, spans, _ = telemetry
    with telemetry.tracer.start_as_current_span("chat qwen3:8b") as span:
        span.set_attribute("gen_ai.input.messages", f"How many rows does {MARKER} have?")
        span.add_event("gen_ai.choice", {"message": MARKER})
        span.set_status(Status(StatusCode.ERROR, f"Table {MARKER} does not exist"))
    exported = spans.get_finished_spans()[0]
    assert (dict(exported.attributes), exported.events, exported.status.description) == ({}, (), None)
    assert exported.status.status_code is StatusCode.ERROR


def test_links_keep_their_target_but_lose_their_attributes(telemetry):
    telemetry, spans, _ = telemetry
    with telemetry.tracer.start_as_current_span("invoke_agent lakehouse-operator") as earlier:
        pass
    link = Link(earlier.get_span_context(), {"reason": f"retry of {MARKER}"})
    with telemetry.tracer.start_as_current_span("invoke_agent lakehouse-operator", links=[link]):
        pass
    (exported_link,) = spans.get_finished_spans()[1].links
    assert exported_link.context == earlier.get_span_context()
    assert dict(exported_link.attributes or {}) == {}


def test_a_failing_tool_records_the_error_class_only(telemetry):
    telemetry, spans, _ = telemetry
    with pytest.raises(LookupError), telemetry.task("qwen3:8b"), telemetry.tool(ToolCall("call_9", "row_count", {})):
        raise LookupError(f"Table {MARKER} does not exist")
    tool, task = spans.get_finished_spans()
    assert (tool.attributes[tracing.ERROR_TYPE], task.attributes[tracing.OUTCOME]) == ("LookupError", "failed")
    assert MARKER not in json.dumps([dict(tool.attributes), dict(task.attributes), tool.status.description])


def test_metrics_carry_the_instrument_names_buckets_and_labels(telemetry):
    telemetry, _, reader = telemetry
    telemetry.prime(models=["qwen3:8b"], action_classes=["delete", "expire_snapshots"])
    run_task(telemetry)
    found = points(reader)

    assert set(found) <= {i.name for i in instruments.ALL}
    unsafe = {p.attributes[tracing.ACTION_CLASS]: p.value for p in found[instruments.UNSAFE_ACTIONS.name]}
    assert unsafe == {"delete": 0, "expire_snapshots": 0}  # primed: the first real increase is visible
    tasks = {p.attributes[tracing.OUTCOME]: p.value for p in found[instruments.TASKS.name]}
    assert tasks["executed"] == 1 and tasks["blocked"] == 0
    assert all(
        p.attributes[tracing.MODE] == "eval" and p.attributes[tracing.ARM] == "guarded"
        for p in found[instruments.TASKS.name]
    )
    (duration,) = found[instruments.TASK_DURATION.name]
    assert tuple(duration.explicit_bounds) == instruments.TASK_DURATION.buckets
    assert duration.attributes[tracing.REQUEST_MODEL] == "qwen3:8b"
    tokens = {p.attributes[tracing.TOKEN_TYPE]: p.count for p in found[instruments.TOKEN_USAGE.name]}
    assert tokens == {"input": 2, "output": 2}
    assert tuple(found[instruments.TOKEN_USAGE.name][0].explicit_bounds) == instruments.TOKEN_USAGE.buckets


def test_unsafe_actions_count_per_action_class(telemetry):
    telemetry, _, reader = telemetry
    telemetry.prime(models=[], action_classes=["delete"])
    telemetry.unsafe_action("delete")
    (point,) = points(reader)[instruments.UNSAFE_ACTIONS.name]
    assert (point.attributes[tracing.ACTION_CLASS], point.value) == ("delete", 1)
