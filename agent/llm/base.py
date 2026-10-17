"""Provider-neutral types of the agent's LLM layer: one model turn, the tools it may call, and the provider contract.

Every provider:
- keeps the conversation append-only: earlier turns are never edited, removed or reordered;
- reports, per turn, the model it asked for and the model that actually served it;
- in `Mode.EVAL`, raises `InvalidRun` when anything other than the configured model served a turn;
- never hands back a tool call from a turn it cannot trust (refused, cut off, or not matching the tool's schema).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import jsonschema


class Mode(StrEnum):
    EVAL = "eval"  # fallback off: a turn served by another model invalidates the run
    DEMO = "demo"  # demos and operations: server-side fallback on, the serving model is recorded


class Stop(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    REFUSAL = "refusal"  # the eval counts it as a block
    MAX_TOKENS = "max_tokens"
    CONTEXT_LIMIT = "context_limit"
    MALFORMED_TOOL_CALL = "malformed_tool_call"  # blocked here and counted separately, never executed


# A conversation ends at these: there is no complete, trustworthy turn to continue from.
TERMINAL = frozenset({Stop.REFUSAL, Stop.MAX_TOKENS, Stop.CONTEXT_LIMIT})


class InvalidRun(Exception):
    """The turn cannot count in an eval: another model served it, or the local model is not the pinned build."""


class ProviderError(Exception):
    pass


class ProviderUnavailable(ProviderError):
    """Rate limit, overload, server error or network failure: the request may succeed later."""


class ProviderRequestError(ProviderError):
    """The backend rejected the request itself (4xx): retrying the same request cannot help."""


class ConversationClosed(ProviderError):
    """A send after a terminal turn (refusal, max_tokens, context limit)."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call. The input schema is closed, so strict decoding and local validation agree."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        check_closed_schema(self.input_schema, self.name)


def check_closed_schema(schema: dict[str, Any], where: str) -> None:
    """Strict tool use needs `additionalProperties: false` and `required` on every object in the schema."""
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False or "required" not in schema:
            raise ValueError(f"{where}: object schemas need additionalProperties: false and required")
        for name, sub in schema.get("properties", {}).items():
            check_closed_schema(sub, f"{where}.{name}")
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        check_closed_schema(schema["items"], f"{where}[]")


def tool_call_is_valid(tools: Sequence[ToolSpec], name: str, arguments: object) -> bool:
    """A known tool, with arguments that match its schema."""
    spec = next((tool for tool in tools if tool.name == name), None)
    if spec is None or not isinstance(arguments, dict):
        return False
    return jsonschema.Draft202012Validator(spec.input_schema).is_valid(arguments)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True)
class Turn:
    stop: Stop
    text: str
    tool_calls: tuple[ToolCall, ...]
    requested_model: str
    served_model: str
    usage: Usage
    cost_usd: float | None  # None: no list price for a model that served this turn
    duration_s: float
    refusal_category: str | None = None
    fallback: bool = False  # a fallback model served (demo mode only)
    malformed_tool_calls: int = 0
    context_limit_hit: bool = False


class Conversation(Protocol):
    def send_user(self, text: str) -> Turn: ...

    def send_tool_results(self, results: Sequence[ToolResult]) -> Turn: ...


class Provider(Protocol):
    name: str
    model: str

    def start(self, system: str, tools: Sequence[ToolSpec]) -> Conversation: ...
