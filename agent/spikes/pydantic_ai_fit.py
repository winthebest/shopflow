# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic-ai-slim[anthropic,openai]==2.57.0", "anthropic==1.15.0", "opentelemetry-sdk==1.45.1"]
# ///
"""Framework spike: does Pydantic AI keep the agent's LLM contract (docs/adr/0104-agent-framework.md)?

Offline checks run Pydantic AI's Anthropic model against agent/tests/fake_anthropic.py, the fake Messages API that
rejects what the API documents as invalid for claude-opus-5-5 (ADR 0103). With --ollama, two live checks run its
Ollama model against the local qwen3:8b, which is unloaded afterwards. No check calls the Claude API.

    uv run --script agent/spikes/pydantic_ai_fit.py [--ollama]
"""

import json
import os
import sys
import time
import warnings
from pathlib import Path

import anthropic
import httpx2
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings, AnthropicStaleThinkingBlockWarning
from pydantic_ai.models.instrumented import InstrumentationSettings, InstrumentedModel
from pydantic_ai.providers.anthropic import AnthropicProvider

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import fake_anthropic as fa  # the fake lives with the agent's tests

MARKER = "lab_ro.shop.orders_7f3a"  # appears in the prompt and the tool input; must not appear in spans
QUESTION = f"How many rows does the table {MARKER} have?"
TOOL_TURNS = (
    fa.Reply([fa.thinking(), fa.tool_use("toolu_1", "row_count", {"table": MARKER})], stop_reason="tool_use"),
    fa.Reply([fa.thinking(), fa.text("1234 rows.")]),
)
SETTINGS = AnthropicModelSettings(max_tokens=16000, anthropic_thinking={"type": "adaptive"}, anthropic_effort="high")
results: list[tuple[str, str, str]] = []


def record(check: str, verdict: str, evidence: str) -> None:
    results.append((check, verdict, evidence))


def claude_agent(
    fake: fa.FakeMessagesAPI, settings=SETTINGS, instructions="You operate a data lakehouse.", tracer=None
):
    http = anthropic.DefaultAsyncHttpxClient(transport=fake.transport)
    client = anthropic.AsyncAnthropic(api_key="fake-key", max_retries=0, http_client=http)
    model = AnthropicModel("claude-opus-5-5", provider=AnthropicProvider(anthropic_client=client))
    if tracer is not None:
        model = InstrumentedModel(model, InstrumentationSettings(tracer_provider=tracer, include_content=False))
    agent = Agent(model, instructions=instructions, model_settings=settings)

    @agent.tool_plain(strict=True)
    def row_count(table: str) -> str:
        """Number of rows in a table of the read-only catalog."""
        return "1234"

    return agent


def check_request_shape() -> None:
    fake = fa.FakeMessagesAPI(*TOOL_TURNS)
    output = claude_agent(fake).run_sync(QUESTION).output
    bodies = [r.body for r in fake.requests]
    shape = {
        "thinking": {json.dumps(b.get("thinking")) for b in bodies},
        "effort": {(b.get("output_config") or {}).get("effort") for b in bodies},
        "tool_choice": {json.dumps(b.get("tool_choice")) for b in bodies},
        "strict": {t.get("strict") for b in bodies for t in b.get("tools", [])},
    }
    ok = shape["tool_choice"] == {'{"type": "auto"}'} and shape["strict"] == {True} and output == "1234 rows."
    record("tool loop: adaptive thinking, effort, strict tools, tool_choice auto", "yes" if ok else "no", str(shape))
    record(
        "history append-only as the API compares it",
        "yes" if fake.dropped_thinking_blocks == 0 else "no",
        f"{len(bodies)} requests accepted by the fake, {fake.dropped_thinking_blocks} thinking blocks dropped",
    )


def check_silent_drop() -> None:
    turn = iter(range(100))

    def instructions() -> str:  # a per-request value: the prefix changes between the two requests
        return f"You operate a data lakehouse. Request {next(turn)}."

    fake = fa.FakeMessagesAPI(*TOOL_TURNS, fa.Reply([fa.text("1234 rows.")]))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        claude_agent(fake, instructions=instructions).run_sync(QUESTION)
    warned = any(issubclass(w.category, AnthropicStaleThinkingBlockWarning) for w in caught)
    record(
        "default: a rejected thinking block is dropped and the request retried",
        "yes (silent unless warnings are errors)",
        f"{fake.dropped_thinking_blocks} block dropped after the documented 400; warning raised: {warned}",
    )

    fake = fa.FakeMessagesAPI(*TOOL_TURNS)
    binding = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}
    try:
        settings = AnthropicModelSettings(**{**SETTINGS, "anthropic_thinking": binding})
        claude_agent(fake, settings, instructions).run_sync(QUESTION)
        record("opt-out: block_binding error fails the run", "no", "the run completed")
    except Exception as exc:  # the spike reports whatever it raises
        record("opt-out: block_binding error fails the run", "yes", f"{type(exc).__name__}: 400, no retry")


def check_fallback_response() -> None:
    fake = fa.FakeMessagesAPI(
        fa.Reply([fa.fallback("claude-opus-5-5", "claude-opus-4-8"), fa.text("ok")], model="claude-opus-4-8")
    )
    settings = AnthropicModelSettings(
        **SETTINGS, extra_body={"fallbacks": "default"}, anthropic_betas=["server-side-fallback-2026-07-01"]
    )
    try:
        claude_agent(fake, settings).run_sync(QUESTION)
        record("demo mode: a response with a `fallback` block", "handled", "")
    except Exception as exc:
        record("demo mode: a response with a `fallback` block", "crashes", f"{type(exc).__name__}: {exc}"[:120])


def check_served_model() -> None:
    fake = fa.FakeMessagesAPI(fa.Reply([fa.text("ok")], model="claude-opus-4-8"))
    response = claude_agent(fake).run_sync(QUESTION).all_messages()[-1]
    record(
        "eval: a turn served by another model is noticed",
        "no (needs our own check)",
        f"run completed; ModelResponse.model_name = {response.model_name}",
    )


def check_otel() -> None:
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    claude_agent(fa.FakeMessagesAPI(*TOOL_TURNS), tracer=tracer).run_sync(QUESTION)
    spans = exporter.get_finished_spans()
    dump = json.dumps([[s.name, dict(s.attributes or {}), [dict(e.attributes or {}) for e in s.events]] for s in spans])
    record(
        "OTel GenAI spans with include_content=False carry no content",
        "no" if MARKER in dump else "yes",
        f"spans {[s.name for s in spans]}; marker in spans: {MARKER in dump}",
    )


def ollama(path: str, body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:11434{path}"
    response = httpx2.post(url, json=body, timeout=600) if body is not None else httpx2.get(url, timeout=10)
    return response.raise_for_status().json()


def check_ollama() -> None:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.ollama import OllamaProvider

    model = OpenAIChatModel("qwen3:8b", provider=OllamaProvider(base_url="http://127.0.0.1:11434/v1"))
    agent = Agent(model, instructions="You operate a data lakehouse. Use the tools to answer; never guess a number.")

    @agent.tool_plain
    def row_count(table: str) -> str:
        """Number of rows in a table of the read-only catalog, e.g. lab_ro.shop.orders."""
        return "1234"

    try:
        started = time.monotonic()
        run = agent.run_sync("How many rows does the table lab_ro.shop.orders have?")
        calls = [p.tool_name for m in run.all_messages() for p in getattr(m, "parts", []) if p.part_kind == "tool-call"]
        loaded = [m.get("context_length") for m in ollama("/api/ps")["models"] if m["name"] == "qwen3:8b"]
        record(
            "Ollama (OpenAI-compatible route): tool loop on qwen3:8b",
            "yes" if calls == ["row_count"] and "1234" in run.output.replace(",", "") else "no",
            f"tool calls {calls}, {time.monotonic() - started:.1f} s, loaded context {loaded}",
        )
        long_prompt = "lorem ipsum dolor " * 3000 + "Reply with the single word OK."
        answer = Agent(model).run_sync(long_prompt).output
        record(
            "Ollama: a prompt over the context fails instead of being cut",
            "no (answered from a cut prompt)" if answer else "yes",
            f"~{len(long_prompt) // 4} tokens sent at context {loaded}; answer {answer.strip()[:40]!r}",
        )
    finally:
        ollama("/api/generate", {"model": "qwen3:8b", "keep_alive": 0})


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    check_request_shape()
    check_silent_drop()
    check_fallback_response()
    check_served_model()
    check_otel()
    if "--ollama" in sys.argv:
        check_ollama()
    width = max(len(check) for check, _, _ in results)
    for check, verdict, evidence in results:
        print(f"{check:<{width}}  {verdict:<40}  {evidence}")


if __name__ == "__main__":
    main()
