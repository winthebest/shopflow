"""Claude through the Anthropic SDK (docs/adr/0103-llm-provider-choice.md).

Each request: adaptive thinking with an explicit effort, strict tools with `tool_choice: auto`, a cache breakpoint on
the system prompt (tools render before it, so it covers both) plus automatic caching of the growing history, and
streaming. The history is append-only: each response's content goes back unchanged, which keeps the cache prefix and
the thinking blocks valid.

`Mode.EVAL` sends no `fallbacks`, and a turn served by any model but the configured one raises `InvalidRun`.
`Mode.DEMO` lets the API re-run a declined request on Anthropic's default fallback and records who served.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anthropic

from agent.llm.base import (
    TERMINAL,
    ConversationClosed,
    InvalidRun,
    Mode,
    ProviderRequestError,
    ProviderUnavailable,
    Stop,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
    Usage,
    tool_call_is_valid,
)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # the beta of the `fallbacks: "default"` form
EPHEMERAL = {"type": "ephemeral"}  # 5-minute TTL


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_read: float


PRICES = {DEFAULT_MODEL: Price(input=4.00, output=20.00, cache_write_5m=5.00, cache_read=0.20)}


def cost_usd(model: str, usage: Any) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + (usage.cache_creation_input_tokens or 0) * price.cache_write_5m
        + (usage.cache_read_input_tokens or 0) * price.cache_read
    ) / 1_000_000


def turn_cost(served_model: str, usage: Any) -> float | None:
    """Every attempt of the turn (`usage.iterations`) at its own model's price; None if one has no known price.

    Not modelled: how a refusal before any output is billed, and fallback credits. Neither occurs in eval mode.
    """
    if not usage.iterations:
        return cost_usd(served_model, usage)
    costs = [cost_usd(iteration.model, iteration) for iteration in usage.iterations]
    return None if None in costs else sum(costs)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        mode: Mode,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_tokens: int = 64_000,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.mode = mode
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.Anthropic()

    def start(self, system: str, tools: Sequence[ToolSpec]) -> "AnthropicConversation":
        return AnthropicConversation(self, system, tools)


class AnthropicConversation:
    def __init__(self, provider: AnthropicProvider, system: str, tools: Sequence[ToolSpec]) -> None:
        self._provider = provider
        self._tools = tuple(tools)
        self._system = [{"type": "text", "text": system, "cache_control": EPHEMERAL}]
        self._tool_params = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema, "strict": True}
            for t in self._tools
        ]
        self._messages: list[dict[str, Any]] = []
        self._closed = False

    def send_user(self, text: str) -> Turn:
        return self._send({"role": "user", "content": text})

    def send_tool_results(self, results: Sequence[ToolResult]) -> Turn:
        # All results of one assistant turn go back in a single user message.
        blocks = [
            {"type": "tool_result", "tool_use_id": r.call.id, "content": r.content, "is_error": r.is_error}
            for r in results
        ]
        return self._send({"role": "user", "content": blocks})

    def _send(self, message: dict[str, Any]) -> Turn:
        if self._closed:
            raise ConversationClosed("the previous turn ended the conversation")
        self._messages.append(message)
        provider = self._provider
        request: dict[str, Any] = {
            "model": provider.model,
            "max_tokens": provider.max_tokens,
            "system": self._system,
            "tools": self._tool_params,
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": provider.effort},
            "cache_control": EPHEMERAL,  # automatic breakpoint on the last block: the history so far
            "messages": self._messages,
        }
        if provider.mode is Mode.DEMO:
            request |= {"fallbacks": "default", "betas": [FALLBACK_BETA]}

        started = time.monotonic()
        try:
            with provider.client.beta.messages.stream(**request) as stream:
                response = stream.get_final_message()
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            raise ProviderUnavailable(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:  # 529 overloaded included
                raise ProviderUnavailable(str(exc)) from exc
            raise ProviderRequestError(str(exc)) from exc
        duration = time.monotonic() - started
        return self._turn(response, duration)

    def _turn(self, response: Any, duration: float) -> Turn:
        provider = self._provider
        fallback = any(block.type == "fallback" for block in response.content) or any(
            iteration.type == "fallback_message" for iteration in response.usage.iterations or []
        )
        if provider.mode is Mode.EVAL and (fallback or response.model != provider.model):
            raise InvalidRun(f"turn served by {response.model}, configured {provider.model}")

        usage = response.usage
        common = {
            "requested_model": provider.model,
            "served_model": response.model,
            "usage": Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            ),
            "cost_usd": turn_cost(response.model, usage),
            "duration_s": duration,
            "fallback": fallback,
        }

        stop = {
            "refusal": Stop.REFUSAL,
            "max_tokens": Stop.MAX_TOKENS,
            "model_context_window_exceeded": Stop.CONTEXT_LIMIT,
            "end_turn": Stop.END_TURN,
            "tool_use": Stop.TOOL_USE,
        }.get(response.stop_reason)
        if stop is None:  # pause_turn, compaction, stop_sequence: features this agent does not use
            raise ProviderRequestError(f"unexpected stop_reason {response.stop_reason!r}")
        if stop in TERMINAL:
            # A refused or cut-off turn may hold partial text or tool input: none of it is used.
            self._closed = True
            category = response.stop_details.category if response.stop_details else None
            return Turn(
                stop=stop,
                text="",
                tool_calls=(),
                refusal_category=category if stop is Stop.REFUSAL else None,
                context_limit_hit=stop is Stop.CONTEXT_LIMIT,
                **common,
            )

        self._messages.append({"role": "assistant", "content": echo_content(response.content)})
        text = "".join(block.text for block in response.content if block.type == "text")
        calls = tuple(
            ToolCall(block.id, block.name, block.input) for block in response.content if block.type == "tool_use"
        )
        malformed = sum(not tool_call_is_valid(self._tools, call.name, call.arguments) for call in calls)
        if malformed:
            stop = Stop.MALFORMED_TOOL_CALL
            return Turn(stop=stop, text=text, tool_calls=(), malformed_tool_calls=malformed, **common)
        return Turn(stop=Stop.TOOL_USE if calls else Stop.END_TURN, text=text, tool_calls=calls, **common)


def echo_content(content: list[Any]) -> list[Any]:
    """The response content to append to the history, unchanged unless a fallback happened mid-output.

    Before the last `fallback` block, only text blocks go back: the declined model's thinking and tool calls belong
    to an attempt the API discarded. The `fallback` markers themselves are dropped.
    """
    boundaries = [i for i, block in enumerate(content) if block.type == "fallback"]
    if not boundaries:
        return list(content)
    last = boundaries[-1]
    return [block for block in content[:last] if block.type == "text"] + list(content[last + 1 :])
