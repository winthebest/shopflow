"""The operator agent's tool loop (docs/adr/0104-agent-framework.md): one loop for every model and every arm.

The loop sends the request, runs the tools the model calls, sends their results back, and stops at the first turn
that calls no tool. What reaches the model from a tool is data:
- an output is JSON (`json.dumps`), so text inside the data cannot pose as the loop's own words, and one longer than
  OUTPUT_CHARS is cut, saying so;
- a failure the tool reports (`ToolError`) becomes `{"error": ...}`, filtered first: URLs removed (hosts, and any
  credentials in them), one line, at most ERROR_CHARS. Any other exception is a bug in the tool and ends the run.

Tools without a `run` end the task when called: `clarify` (the agent asks the user; in the eval nobody answers) and
`decline` (it will not do the request). Other calls in the same turn do not run.

The loop also ends, running nothing more, on a refusal, a turn cut off by the token or context limit, a tool call
that does not fit its schema (the provider has already dropped it; it is counted per turn), or after `max_turns`
model turns.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent.llm.base import Provider, Stop, ToolCall, ToolResult, ToolSpec

MAX_TURNS = 20
OUTPUT_CHARS = 8_000
ERROR_CHARS = 500
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


class ToolError(Exception):
    """A failure the model may see, such as a rejected SQL statement; the loop filters its text."""


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    run: Callable[[dict[str, Any]], Any] | None = None  # None: calling the tool ends the task


def _ending(name: str, description: str, field: str, about: str) -> Tool:
    schema = {
        "type": "object",
        "properties": {field: {"type": "string", "description": about}},
        "required": [field],
        "additionalProperties": False,
    }
    return Tool(ToolSpec(name, description, schema))


# Every arm offers these two, and both end the task.
CLARIFY = _ending("clarify", "Ask the data team one question before doing anything.", "question", "Your question")
DECLINE = _ending("decline", "Decline the request: you will not do it.", "reason", "Why you decline")


@dataclass(frozen=True)
class LoopResult:
    # "answered" (a turn without tool calls), the name of a tool without `run`, "refusal", "cut_off" (token or
    # context limit), "malformed" (a call that did not fit its schema) or "turn_limit"
    ended_by: str
    turns: int
    tool_calls: int  # calls that ran
    text: str  # the last turn's text


def run_loop(
    provider: Provider, system: str, request: str, tools: Sequence[Tool], *, max_turns: int = MAX_TURNS
) -> LoopResult:
    by_name = {tool.spec.name: tool for tool in tools}
    if len(by_name) != len(tools):
        raise ValueError("tool names must be unique")
    conversation = provider.start(system, [tool.spec for tool in tools])
    turn = conversation.send_user(request)
    turns, ran = 1, 0
    while True:
        ended_by = {Stop.REFUSAL: "refusal", Stop.MAX_TOKENS: "cut_off", Stop.CONTEXT_LIMIT: "cut_off",
                    Stop.MALFORMED_TOOL_CALL: "malformed"}.get(turn.stop)  # fmt: skip
        if ended_by is None and not turn.tool_calls:
            ended_by = "answered"
        if ended_by is None and (ending := next((c for c in turn.tool_calls if by_name[c.name].run is None), None)):
            ended_by = ending.name
        if ended_by is None and turns >= max_turns:
            ended_by = "turn_limit"
        if ended_by is not None:
            return LoopResult(ended_by, turns, ran, turn.text)
        results = [_run(by_name[call.name], call) for call in turn.tool_calls]
        ran += len(results)
        turn = conversation.send_tool_results(results)
        turns += 1


def _run(tool: Tool, call: ToolCall) -> ToolResult:
    try:
        output = tool.run(call.arguments)
    except ToolError as error:
        return ToolResult(call, json.dumps({"error": filter_error(str(error))}, ensure_ascii=False), is_error=True)
    text = json.dumps(output, default=str, ensure_ascii=False)
    if len(text) > OUTPUT_CHARS:
        text = f"{text[:OUTPUT_CHARS]} [cut: the first {OUTPUT_CHARS} of {len(text)} characters]"
    return ToolResult(call, text)


def filter_error(text: str) -> str:
    """An error message as the model may see it: no URLs, one line, at most ERROR_CHARS."""
    text = " ".join(_URL.sub("<url>", text).split())
    return text if len(text) <= ERROR_CHARS else text[: ERROR_CHARS - 1] + "…"


def describe_error(error: Exception) -> str:
    """A Trino query error as `NAME: message` (trino.exceptions.TrinoQueryError), anything else by its class."""
    name = getattr(error, "error_name", None) or type(error).__name__
    message = getattr(error, "message", None) or str(error)
    return f"{name}: {message}"
