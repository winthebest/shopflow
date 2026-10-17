"""A stand-in for the Messages API that answers like the real one, refusals of invalid requests included.

It streams scripted replies as server-sent events, the way `client.beta.messages.stream()` reads them, and answers
400 to what the API documents as invalid for claude-opus-5-5 (sources in docs/adr/0103-llm-provider-choice.md):
thinking other than adaptive (`disabled`, `budget_tokens`), sampling parameters, an unknown effort, forced
`tool_choice`, a strict tool whose schema is not closed, a `fallbacks` form without its own beta header, assistant
prefill, tool results that do not answer the previous turn's tool calls, more than four cache breakpoints, and an
edited history: a request that carries a thinking block this server issued must repeat the system prompt, the tools
and every earlier message exactly as they were when the block was produced.

The checks are written here independently of `agent.llm`, so a mistake in the adapter cannot hide in a shared helper.
Error messages quote the docs where the docs give the text; elsewhere they only name the field.
"""

import itertools
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx2

MODEL = "claude-opus-5-5"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
FALLBACK_BETAS = {"default": "server-side-fallback-2026-07-01", "list": "server-side-fallback-2026-06-01"}
API_BLOCK_FIELDS = {
    "text": ("text",),
    "thinking": ("thinking", "signature"),
    "tool_use": ("id", "name", "input"),
}


@dataclass
class Reply:
    content: list[dict[str, Any]]
    stop_reason: str = "end_turn"
    model: str = MODEL
    stop_details: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=lambda: {"input_tokens": 1200, "output_tokens": 300})
    status: int = 200  # anything else: an error response of `error_type`
    error_type: str = "api_error"


@dataclass
class Recorded:
    headers: httpx2.Headers
    body: dict[str, Any]


class Rejected(Exception):
    pass


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(id_: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": name, "input": arguments}


_signatures = itertools.count(1)


def thinking() -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": f"sig-{next(_signatures)}"}


def fallback(from_model: str, to_model: str, category: str = "cyber") -> dict[str, Any]:
    return {
        "type": "fallback",
        "from": {"model": from_model},
        "to": {"model": to_model},
        "trigger": {"type": "refusal", "category": category},
    }


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def echo_form(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A turn as the client must send it back: after a mid-output fallback, only text blocks from before the last
    `fallback` block (the declined attempt's thinking and tool calls are omitted); the marker itself may be dropped."""
    content = [api_block(b) for b in content]
    last = max((i for i, b in enumerate(content) if b["type"] == "fallback"), default=None)
    if last is None:
        return content
    return [b for b in content[:last] if b["type"] == "text"] + [
        b for b in content[last + 1 :] if b["type"] != "fallback"
    ]


def api_block(block: dict[str, Any]) -> dict[str, Any]:
    """The fields the API defines for a block type; anything else a client sends along is not compared."""
    keys = API_BLOCK_FIELDS.get(block["type"])
    return block if keys is None else {"type": block["type"], **{k: block.get(k) for k in keys}}


class FakeMessagesAPI:
    def __init__(self, *replies: Reply) -> None:
        self.replies = deque(replies)
        self.requests: list[Recorded] = []
        self._issued: dict[str, tuple[str, str]] = {}  # signature -> (prefix, the assistant turn) as issued
        self._ids = itertools.count(1)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(Recorded(request.headers, body))
        if body.get("model") != MODEL:
            return error(404, "not_found_error", f"model: {body.get('model')}")
        try:
            validate(request.headers, body, self._issued)
        except Rejected as exc:
            return error(400, "invalid_request_error", str(exc))
        reply = self.replies.popleft()
        if reply.status != 200:
            return error(reply.status, reply.error_type, "scripted error")
        assert body.get("stream") is True, "this fake only streams"
        prefix = canonical({"system": body.get("system"), "tools": body.get("tools"), "messages": body["messages"]})
        turn = canonical(echo_form(reply.content))
        for block in reply.content:
            if block["type"] == "thinking":
                self._issued[block["signature"]] = (prefix, turn)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(reply, next(self._ids)))


def error(status: int, kind: str, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": message}})


def validate(headers: httpx2.Headers, body: dict[str, Any], issued: dict[str, tuple[str, str]]) -> None:
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 128_000:
        raise Rejected("max_tokens: must be between 1 and 128000")
    for name in ("temperature", "top_p", "top_k"):
        if name in body:
            raise Rejected(f"{name}: not supported for this model")
    thinking_type = (body.get("thinking") or {"type": "adaptive"}).get("type")
    if thinking_type != "adaptive":
        raise Rejected(
            f'"thinking.type.{thinking_type}" is not supported for this model. '
            'Use "thinking.type.adaptive" and "output_config.effort" to control thinking behavior.'
        )
    effort = (body.get("output_config") or {}).get("effort")
    if effort is not None and effort not in EFFORTS:
        raise Rejected(f"output_config.effort: unknown value {effort!r}")
    if (body.get("tool_choice") or {}).get("type") in ("any", "tool"):
        raise Rejected('tool_choice: type "tool" and "any" are not supported for this model.')
    for tool in body.get("tools") or []:
        if tool.get("strict") and not closed(tool.get("input_schema", {})):
            raise Rejected(f"tools.{tool.get('name')}: strict needs additionalProperties: false and required")
    if "fallbacks" in body:
        form = "default" if body["fallbacks"] == "default" else "list"
        if FALLBACK_BETAS[form] not in headers.get("anthropic-beta", ""):
            raise Rejected(f"fallbacks: the {form} form needs the {FALLBACK_BETAS[form]} beta header")
    check_messages(body.get("messages") or [])
    if breakpoints(body) > 4:
        raise Rejected("cache_control: more than 4 breakpoints")
    check_history(body, issued)


def closed(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False or "required" not in schema:
            return False
        return all(closed(sub) for sub in schema.get("properties", {}).values())
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return closed(schema["items"])
    return True


def blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message["content"]
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def check_messages(messages: list[dict[str, Any]]) -> None:
    if not messages or messages[0]["role"] != "user":
        raise Rejected("messages: the first message must use the user role")
    if messages[-1]["role"] == "assistant":
        raise Rejected("messages: assistant prefill is not supported for this model")
    # Tool use docs, "Handle tool calls": results immediately follow their tool use, and come first in the content.
    for i, message in enumerate(messages):
        kinds = [b["type"] for b in blocks(message)]
        if "tool_result" in kinds:
            last_result = max(j for j, kind in enumerate(kinds) if kind == "tool_result")
            if any(kind != "tool_result" for kind in kinds[:last_result]):
                raise Rejected(f"messages.{i}: tool_result blocks must come first in the content")
        results = {b["tool_use_id"] for b in blocks(message) if b["type"] == "tool_result"}
        asked = set()
        if i > 0 and messages[i - 1]["role"] == "assistant":
            asked = {b["id"] for b in blocks(messages[i - 1]) if b["type"] == "tool_use"}
        if results - asked:
            raise Rejected(f"messages.{i}: tool_result blocks without a tool_use in the previous message")
        if message["role"] == "assistant":
            calls = {b["id"] for b in blocks(message) if b["type"] == "tool_use"}
            following = blocks(messages[i + 1]) if i + 1 < len(messages) else []
            if calls - {b.get("tool_use_id") for b in following if b["type"] == "tool_result"}:
                raise Rejected("tool_use ids were found without tool_result blocks immediately after")


def breakpoints(body: dict[str, Any]) -> int:
    marked = [*(body.get("tools") or []), *(body.get("system") or [])]
    for message in body.get("messages") or []:
        marked.extend(blocks(message))
    return sum("cache_control" in item for item in marked) + ("cache_control" in body)


def check_history(body: dict[str, Any], issued: dict[str, tuple[str, str]]) -> None:
    messages = body["messages"]
    for i, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        for block in blocks(message):
            if block["type"] != "thinking" or block.get("signature") not in issued:
                continue
            prefix, turn = issued[block["signature"]]
            now = canonical({"system": body.get("system"), "tools": body.get("tools"), "messages": messages[:i]})
            if now != prefix or canonical(echo_form(blocks(message))) != turn:
                raise Rejected(f"messages.{i}: thinking block from an edited conversation")


def sse(reply: Reply, n: int) -> bytes:
    usage = reply.usage
    start_usage = {k: usage.get(k, 0) for k in ("cache_creation_input_tokens", "cache_read_input_tokens")}
    events = [
        (
            "message_start",
            {
                "message": {
                    "id": f"msg_fake_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": reply.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 1, **start_usage},
                }
            },
        )
    ]
    for index, block in enumerate(reply.content):
        kind = block["type"]
        if kind == "text":
            events.append(("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}}))
            delta = {"type": "text_delta", "text": block["text"]}
            events.append(("content_block_delta", {"index": index, "delta": delta}))
        elif kind == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            events.append(("content_block_start", {"index": index, "content_block": start}))
            delta = {"type": "signature_delta", "signature": block["signature"]}
            events.append(("content_block_delta", {"index": index, "delta": delta}))
        elif kind == "tool_use":
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            events.append(("content_block_start", {"index": index, "content_block": start}))
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            events.append(("content_block_delta", {"index": index, "delta": delta}))
        else:  # fallback: arrives whole
            events.append(("content_block_start", {"index": index, "content_block": block}))
        events.append(("content_block_stop", {"index": index}))
    delta = {"stop_reason": reply.stop_reason, "stop_sequence": None, "stop_details": reply.stop_details}
    events.append(("message_delta", {"delta": delta, "usage": usage}))
    events.append(("message_stop", {}))
    return "".join(f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n" for name, data in events).encode()
