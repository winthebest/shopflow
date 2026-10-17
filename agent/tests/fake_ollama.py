"""A stand-in for Ollama's native API that behaves like the real server (0.32.0, measured 2026-10-18).

- An unknown model is a 404 `{"error": "model '<name>' not found"}`.
- A prompt longer than `num_ctx` is silently cut unless the request sets `truncate: false`, in which case it is a 400
  `exceed_context_size_error` naming `n_prompt_tokens` and `n_ctx`. Token counts here are estimated (4 characters a
  token), which is enough to decide whether a scripted prompt fits.
- `prompt_eval_count` counts the whole prompt, cached prefix included; `model` echoes the requested name.
"""

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx2

DIGEST = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    done_reason: str = "stop"
    eval_count: int = 40


def call(name: str, arguments: Any, id_: str = "call_1") -> dict[str, Any]:
    return {"id": id_, "function": {"index": 0, "name": name, "arguments": arguments}}


def prompt_tokens(body: dict[str, Any]) -> int:
    return len(json.dumps(body["messages"])) // 4 + len(json.dumps(body.get("tools", []))) // 4


class FakeOllama:
    def __init__(self, *replies: Reply, digest: str = DIGEST, quantization: str = "Q4_K_M") -> None:
        self.replies = deque(replies)
        self.models = {"qwen3:8b": {"digest": digest, "quantization_level": quantization}}
        self.requests: list[dict[str, Any]] = []
        self.unloaded: list[str] = []

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/tags":
            models = [
                {"name": name, "digest": m["digest"], "details": {"quantization_level": m["quantization_level"]}}
                for name, m in self.models.items()
            ]
            return httpx2.Response(200, json={"models": models})
        body = json.loads(request.content)
        if body.get("model") not in self.models:
            return httpx2.Response(404, json={"error": f"model '{body.get('model')}' not found"})
        if request.url.path == "/api/generate" and body.get("keep_alive") == 0:
            self.unloaded.append(body["model"])
            return httpx2.Response(200, json={"model": body["model"], "done": True, "done_reason": "unload"})
        assert request.url.path == "/api/chat", request.url.path
        self.requests.append(body)
        num_ctx = body.get("options", {}).get("num_ctx", 4096)
        tokens = prompt_tokens(body)
        if tokens > num_ctx:
            if body.get("truncate") is False:
                inner = {
                    "error": {
                        "code": 400,
                        "message": f"request ({tokens} tokens) exceeds the available context size ({num_ctx} tokens),"
                        " try increasing it",
                        "type": "exceed_context_size_error",
                        "n_prompt_tokens": tokens,
                        "n_ctx": num_ctx,
                    }
                }
                return httpx2.Response(400, json={"error": json.dumps(inner)})
            tokens = num_ctx // 2  # silently cut, as the real server does
        reply = self.replies.popleft()
        message = {"role": "assistant", "content": reply.content, "thinking": "..."}
        if reply.tool_calls:
            message["tool_calls"] = reply.tool_calls
        return httpx2.Response(
            200,
            json={
                "model": body["model"],
                "message": message,
                "done": True,
                "done_reason": reply.done_reason,
                "prompt_eval_count": tokens,
                "eval_count": reply.eval_count,
            },
        )
