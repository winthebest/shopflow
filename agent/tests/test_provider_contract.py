"""One contract for every LLM provider.

Offline, always (CI included): against fakes that answer and reject like the real backends (fake_anthropic.py,
fake_ollama.py), so a passing test also proves the request shapes. Live: the Claude API only with ANTHROPIC_API_KEY
set (it spends money), and a local Ollama with the pinned model (`make eval-agent-test-ollama`); otherwise those
tests are skipped and say why.
"""

import json
import os
from typing import Any

import anthropic
import fake_anthropic as fa
import fake_ollama as fo
import httpx2
import pytest

from agent.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider, turn_cost
from agent.llm.base import (
    ConversationClosed,
    InvalidRun,
    Mode,
    ProviderRequestError,
    ProviderUnavailable,
    Stop,
    ToolResult,
    ToolSpec,
)
from agent.llm.ollama_provider import DEFAULT_URL, QWEN3_8B, OllamaProvider

SYSTEM = "You operate a data lakehouse. Use the tools to answer; never guess a number."
ROW_COUNT = ToolSpec(
    "row_count",
    "Number of rows in a table of the read-only catalog, e.g. lab_ro.shop.orders",
    {
        "type": "object",
        "properties": {"table": {"type": "string", "description": "catalog.schema.table"}},
        "required": ["table"],
        "additionalProperties": False,
    },
)
QUESTION = "How many rows does the table lab_ro.shop.orders have?"
ARGS = {"table": "lab_ro.shop.orders"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def claude(fake: fa.FakeMessagesAPI, mode: Mode = Mode.EVAL, **kwargs: Any) -> AnthropicProvider:
    http = anthropic.DefaultHttpxClient(transport=fake.transport)
    client = anthropic.Anthropic(api_key="fake-key", max_retries=0, http_client=http)
    return AnthropicProvider(mode=mode, client=client, **kwargs)


def ollama(fake: fo.FakeOllama, **kwargs: Any) -> OllamaProvider:
    return OllamaProvider(http=httpx2.Client(base_url="http://ollama.test", transport=fake.transport), **kwargs)


def claude_round_trip():
    fake = fa.FakeMessagesAPI(
        fa.Reply([fa.thinking(), fa.tool_use("toolu_1", "row_count", ARGS)], stop_reason="tool_use"),
        fa.Reply([fa.thinking(), fa.text("1234 rows.")]),
    )
    return claude(fake), lambda: [r.body["messages"] for r in fake.requests]


def ollama_round_trip():
    fake = fo.FakeOllama(fo.Reply(tool_calls=[fo.call("row_count", ARGS)]), fo.Reply(content="1234 rows."))
    return ollama(fake), lambda: [r["messages"] for r in fake.requests]


BACKENDS = {"anthropic": claude_round_trip, "ollama": ollama_round_trip}


# --- the contract, on both providers --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_tool_round_trip_with_an_append_only_history(backend):
    provider, sent = BACKENDS[backend]()
    conversation = provider.start(SYSTEM, [ROW_COUNT])

    turn = conversation.send_user(QUESTION)
    assert turn.stop is Stop.TOOL_USE
    assert [(c.name, c.arguments) for c in turn.tool_calls] == [("row_count", ARGS)]
    assert turn.served_model == turn.requested_model == provider.model
    final = conversation.send_tool_results([ToolResult(turn.tool_calls[0], "1234")])
    assert (final.stop, final.text, final.tool_calls) == (Stop.END_TURN, "1234 rows.", ())
    assert final.usage.input_tokens > 0
    assert final.cost_usd is not None

    first, second = sent()
    assert canonical(second[: len(first)]) == canonical(first)  # earlier messages go back unchanged
    assert [m["role"] for m in second[len(first) :]] == ["assistant", "user" if backend == "anthropic" else "tool"]


def test_an_edited_history_is_rejected():
    """The fake enforces what the API enforces on thinking blocks: an edit of an earlier turn is a 400."""
    provider, _ = claude_round_trip()
    conversation = provider.start(SYSTEM, [ROW_COUNT])
    turn = conversation.send_user(QUESTION)
    conversation._messages[0]["content"] = "How many rows does lab_ro.shop.payments have?"

    with pytest.raises(ProviderRequestError, match="edited conversation"):
        conversation.send_tool_results([ToolResult(turn.tool_calls[0], "1234")])


def test_tool_specs_must_be_closed():
    with pytest.raises(ValueError, match="additionalProperties"):
        ToolSpec("row_count", "", {"type": "object", "properties": {"table": {"type": "string"}}, "required": []})


# --- Claude API -------------------------------------------------------------------------------------------------


def test_eval_request_has_no_fallback_and_the_documented_shape():
    fake = fa.FakeMessagesAPI(fa.Reply([fa.text("ok")]))
    claude(fake, effort="medium").start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)

    request = fake.requests[0]
    body = request.body
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["tool_choice"] == {"type": "auto"}
    assert body["tools"][0]["strict"] is True
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}  # covers tools + system
    assert body["cache_control"] == {"type": "ephemeral"}  # automatic: the history
    assert body["stream"] is True
    assert "fallbacks" not in body
    assert "server-side-fallback" not in request.headers.get("anthropic-beta", "")


def test_demo_request_asks_for_the_default_fallback():
    fake = fa.FakeMessagesAPI(fa.Reply([fa.text("ok")]))
    claude(fake, mode=Mode.DEMO).start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)

    assert fake.requests[0].body["fallbacks"] == "default"
    assert FALLBACK_BETA in fake.requests[0].headers["anthropic-beta"]


def test_a_turn_served_by_another_model_invalidates_an_eval():
    fake = fa.FakeMessagesAPI(fa.Reply([fa.text("ok")], model="claude-opus-4-8"))
    with pytest.raises(InvalidRun, match="claude-opus-4-8"):
        claude(fake).start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)


def test_demo_mode_records_the_fallback_and_echoes_only_the_kept_blocks():
    first_attempt = {"type": "message", "model": "claude-opus-5-5", "input_tokens": 900, "output_tokens": 30}
    rescue = {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 950, "output_tokens": 80}
    usage = {"input_tokens": 950, "output_tokens": 80, "iterations": [first_attempt, rescue]}
    content = [
        fa.thinking(),
        fa.text("Checking "),
        fa.fallback("claude-opus-5-5", "claude-opus-4-8"),
        fa.thinking(),
        fa.text("the count."),
    ]
    fake = fa.FakeMessagesAPI(fa.Reply(content, model="claude-opus-4-8", usage=usage), fa.Reply([fa.text("ok")]))
    conversation = claude(fake, mode=Mode.DEMO).start(SYSTEM, [ROW_COUNT])

    turn = conversation.send_user(QUESTION)
    assert (turn.fallback, turn.served_model, turn.requested_model) == (True, "claude-opus-4-8", "claude-opus-5-5")
    assert turn.cost_usd is None  # no list price here for the fallback model
    conversation.send_user("Thanks.")
    echoed = fake.requests[1].body["messages"][1]["content"]
    assert [b["type"] for b in echoed] == ["text", "thinking", "text"]


def test_refusal_is_a_terminal_turn_without_tool_calls():
    details = {"type": "refusal", "category": "cyber", "explanation": None}
    partial = [fa.tool_use("toolu_1", "row_count", ARGS)]
    fake = fa.FakeMessagesAPI(fa.Reply(partial, stop_reason="refusal", stop_details=details))
    conversation = claude(fake).start(SYSTEM, [ROW_COUNT])

    turn = conversation.send_user(QUESTION)
    assert (turn.stop, turn.refusal_category, turn.tool_calls, turn.text) == (Stop.REFUSAL, "cyber", (), "")
    with pytest.raises(ConversationClosed):
        conversation.send_user("Try again.")


@pytest.mark.parametrize(
    ("stop_reason", "stop", "context_limit_hit"),
    [("max_tokens", Stop.MAX_TOKENS, False), ("model_context_window_exceeded", Stop.CONTEXT_LIMIT, True)],
)
def test_a_cut_off_turn_is_terminal(stop_reason, stop, context_limit_hit):
    fake = fa.FakeMessagesAPI(fa.Reply([fa.tool_use("toolu_1", "row_count", ARGS)], stop_reason=stop_reason))
    turn = claude(fake).start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)
    assert (turn.stop, turn.tool_calls, turn.context_limit_hit) == (stop, (), context_limit_hit)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (fa.Reply([], status=429, error_type="rate_limit_error"), ProviderUnavailable),
        (fa.Reply([], status=529, error_type="overloaded_error"), ProviderUnavailable),
        (fa.Reply([], status=500, error_type="api_error"), ProviderUnavailable),
    ],
    ids=["rate-limit", "overloaded", "server-error"],
)
def test_backend_failures_are_typed(reply, error):
    with pytest.raises(error):
        claude(fa.FakeMessagesAPI(reply)).start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)


def test_a_rejected_request_is_not_retryable():
    fake = fa.FakeMessagesAPI(fa.Reply([fa.text("ok")]))
    with pytest.raises(ProviderRequestError, match="effort"):
        claude(fake, effort="extreme").start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)


def test_cost_uses_the_list_prices_of_every_attempt():
    usage = anthropic.types.beta.BetaUsage(
        input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=3000
    )
    assert turn_cost("claude-opus-5-5", usage) == pytest.approx((1000 * 4 + 500 * 20 + 3000 * 5 + 2000 * 0.2) / 1e6)
    assert turn_cost("claude-opus-4-8", usage) is None


VALID = {
    "model": "claude-opus-5-5",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": QUESTION}],
}
STRICT_TOOL = {"name": ROW_COUNT.name, "input_schema": ROW_COUNT.input_schema, "strict": True}
TOOL_TURN = [
    {"role": "user", "content": QUESTION},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "row_count", "input": ARGS}]},
]
TEXT = {"type": "text", "text": "Here is the result:"}
ANSWER = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "1234"}
ORPHAN_RESULT = {"type": "tool_result", "tool_use_id": "toolu_9", "content": "1"}
OPEN_TOOL = {"name": "row_count", "input_schema": {"type": "object", "properties": {}}, "strict": True}


@pytest.mark.parametrize(
    ("changes", "betas", "rejected_for"),
    [
        ({"thinking": {"type": "disabled"}}, [], "thinking.type.disabled"),
        ({"thinking": {"type": "enabled", "budget_tokens": 2048}}, [], "thinking.type.enabled"),
        ({"extra_body": {"temperature": 0.2}}, [], "temperature"),
        ({"tool_choice": {"type": "any"}, "tools": [STRICT_TOOL]}, [], "tool_choice"),
        ({"tool_choice": {"type": "tool", "name": "row_count"}}, [], "tool_choice"),
        ({"tools": [OPEN_TOOL]}, [], "strict"),
        ({"fallbacks": "default"}, [], "fallbacks"),
        ({"fallbacks": "default"}, ["server-side-fallback-2026-06-01"], "fallbacks"),
        ({"fallbacks": [{"model": "claude-opus-4-8"}]}, [FALLBACK_BETA], "fallbacks"),
        ({"messages": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "It is"}]}, [], "prefill"),
        ({"messages": [{"role": "user", "content": [ORPHAN_RESULT]}]}, [], "tool_result"),
        ({"messages": [*TOOL_TURN, {"role": "user", "content": [TEXT, ANSWER]}]}, [], "come first"),
        ({"messages": [*TOOL_TURN, {"role": "user", "content": "1234"}]}, [], "without tool_result blocks"),
        (
            {
                "system": [{"type": "text", "text": str(i), "cache_control": {"type": "ephemeral"}} for i in range(4)],
                "cache_control": {"type": "ephemeral"},
            },
            [],
            "cache_control",
        ),
    ],
    ids=[
        "thinking-disabled",
        "budget-tokens",
        "sampling",
        "tool-choice-any",
        "tool-choice-tool",
        "strict-open-schema",
        "fallbacks-without-beta",
        "default-fallbacks-wrong-beta",
        "listed-fallbacks-wrong-beta",
        "prefill",
        "orphan-tool-result",
        "text-before-tool-result",
        "unanswered-tool-use",
        "five-breakpoints",
    ],
)
def test_the_fake_rejects_what_the_api_rejects(changes, betas, rejected_for):
    fake = fa.FakeMessagesAPI(fa.Reply([fa.text("ok")]))
    client = claude(fake).client
    request = VALID | changes | ({"betas": betas} if betas else {})
    with pytest.raises(anthropic.BadRequestError, match=rejected_for), client.beta.messages.stream(**request) as s:
        s.get_final_message()


def test_the_fake_accepts_the_valid_request():
    fake = fa.FakeMessagesAPI(fa.Reply([fa.text("ok")]))
    with claude(fake).client.beta.messages.stream(**VALID) as stream:
        assert stream.get_final_message().content[0].text == "ok"


# --- Ollama -----------------------------------------------------------------------------------------------------


def test_ollama_request_sets_context_sampling_seed_and_no_truncation():
    fake = fo.FakeOllama(fo.Reply(content="ok"))
    ollama(fake, seed=7).start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)

    body = fake.requests[0]
    sampling = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
    assert body["options"] == {"num_ctx": 16384, "seed": 7, **sampling}
    assert (body["think"], body["truncate"], body["stream"]) == (True, False, False)
    assert body["messages"][0] == {"role": "system", "content": SYSTEM}
    assert body["tools"][0]["function"]["parameters"] == ROW_COUNT.input_schema


@pytest.mark.parametrize(
    "fake",
    [
        fo.FakeOllama(digest="0" * 64),
        fo.FakeOllama(quantization="Q8_0"),
    ],
    ids=["other-digest", "other-quantization"],
)
def test_ollama_model_must_be_the_pinned_build(fake):
    with pytest.raises(InvalidRun, match="pinned"):
        ollama(fake).start(SYSTEM, [ROW_COUNT])


def test_ollama_model_must_be_pulled():
    fake = fo.FakeOllama()
    fake.models = {}
    with pytest.raises(InvalidRun, match="not pulled"):
        ollama(fake).start(SYSTEM, [ROW_COUNT])


@pytest.mark.parametrize(
    "reply",
    [
        fo.Reply(tool_calls=[fo.call("row_count", {"table": 5})]),
        fo.Reply(tool_calls=[fo.call("row_count", {"table": "t", "where": "1=1"})]),
        fo.Reply(tool_calls=[fo.call("drop_table", ARGS)]),
        fo.Reply(content='<tool_call>{"name": "row_count", "arguments": {"table": }</tool_call>'),
    ],
    ids=["wrong-type", "extra-argument", "unknown-tool", "unparsed-markup"],
)
def test_ollama_malformed_tool_call_is_blocked_and_counted(reply):
    turn = ollama(fo.FakeOllama(reply)).start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)
    assert (turn.stop, turn.tool_calls, turn.malformed_tool_calls) == (Stop.MALFORMED_TOOL_CALL, (), 1)


def test_ollama_prompt_over_the_context_is_a_counted_limit_not_a_silent_cut():
    conversation = ollama(fo.FakeOllama(), num_ctx=2048).start(SYSTEM, [ROW_COUNT])
    turn = conversation.send_user("lorem ipsum " * 2000)
    assert (turn.stop, turn.context_limit_hit) == (Stop.CONTEXT_LIMIT, True)
    with pytest.raises(ConversationClosed):
        conversation.send_user(QUESTION)


def test_ollama_context_filling_up_while_generating_is_a_limit():
    reply = fo.Reply(content="...", done_reason="length", eval_count=16_384)
    turn = ollama(fo.FakeOllama(reply)).start(SYSTEM, [ROW_COUNT]).send_user(QUESTION)
    assert (turn.stop, turn.context_limit_hit) == (Stop.CONTEXT_LIMIT, True)


def test_ollama_unload():
    fake = fo.FakeOllama()
    ollama(fake).unload()
    assert fake.unloaded == ["qwen3:8b"]


# --- live backends ----------------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY is not set: the live Claude API contract spends money and runs only with a key",
)
def test_live_claude_round_trip_in_eval_mode():
    conversation = AnthropicProvider(mode=Mode.EVAL, effort="low").start(SYSTEM, [ROW_COUNT])
    turn = conversation.send_user(QUESTION)
    assert turn.stop is Stop.TOOL_USE
    assert turn.served_model == "claude-opus-5-5"
    final = conversation.send_tool_results([ToolResult(turn.tool_calls[0], "1234")])
    assert final.stop is Stop.END_TURN
    assert final.cost_usd is not None


def local_ollama_problem() -> str | None:
    try:
        tags = httpx2.get(f"{DEFAULT_URL}/api/tags", timeout=2).json()
    except httpx2.HTTPError:
        return f"Ollama is not running at {DEFAULT_URL}"
    if not any(m.get("digest") == QWEN3_8B.digest for m in tags.get("models", [])):
        return f"{QWEN3_8B.name} at the pinned digest is not pulled"
    return None


OLLAMA_PROBLEM = local_ollama_problem()
live_ollama = pytest.mark.skipif(OLLAMA_PROBLEM is not None, reason=f"live Ollama contract: {OLLAMA_PROBLEM}")


@pytest.fixture(scope="module")
def local_qwen():
    provider = OllamaProvider()
    yield provider
    provider.unload()  # the model holds ~5 GB until Ollama's keep-alive expires


@pytest.mark.ollama
@live_ollama
def test_live_ollama_round_trip(local_qwen):
    conversation = local_qwen.start(SYSTEM, [ROW_COUNT])
    turn = conversation.send_user(QUESTION)
    assert turn.stop is Stop.TOOL_USE, turn
    assert turn.tool_calls[0].name == "row_count"
    assert turn.tool_calls[0].arguments["table"].endswith("orders")
    final = conversation.send_tool_results([ToolResult(turn.tool_calls[0], "1234")])
    assert final.stop is Stop.END_TURN, final
    assert "1234" in final.text.replace(",", "")

    loaded = httpx2.get(f"{DEFAULT_URL}/api/ps", timeout=5).json()["models"]
    assert [m["context_length"] for m in loaded if m["name"] == QWEN3_8B.name] == [16384]  # num_ctx took effect


@pytest.mark.ollama
@live_ollama
def test_live_ollama_prompt_over_the_context_is_a_counted_limit():
    provider = OllamaProvider(num_ctx=2048)
    try:
        turn = provider.start(SYSTEM, [ROW_COUNT]).send_user("lorem ipsum dolor " * 1500 + QUESTION)
    finally:
        provider.unload()
    assert (turn.stop, turn.context_limit_hit) == (Stop.CONTEXT_LIMIT, True)
