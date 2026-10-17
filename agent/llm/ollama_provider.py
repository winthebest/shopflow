"""A local model through Ollama's native chat API (docs/adr/0103-llm-provider-choice.md).

The model is pinned by digest and quantization and checked before the first turn, the local analogue of asserting
the served model. Each request sets the context size and the model card's sampling with a fixed seed, and
`truncate: false`: by default Ollama silently drops the start of a prompt that does not fit (measured on 0.32.0: a
4,515-token prompt with `num_ctx` 2048 was answered from 1,026 tokens), while with `truncate: false` it refuses with
`exceed_context_size_error`, which becomes a counted context-limit turn.

The KV cache type is a server setting, not a request option: the brew service runs with OLLAMA_FLASH_ATTENTION=1 and
OLLAMA_KV_CACHE_TYPE=q8_0 (the ADR shows how to check). There is no fallback model, so the pin is checked in every mode.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx2

from agent.llm.base import (
    ConversationClosed,
    InvalidRun,
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

DEFAULT_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class LocalModel:
    name: str
    digest: str  # `ollama list` / GET /api/tags
    quantization: str


QWEN3_8B = LocalModel(
    name="qwen3:8b", digest="500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41", quantization="Q4_K_M"
)
# Qwen3 model card, thinking mode: temperature 0.6, top_p 0.95, top_k 20, min_p 0; greedy decoding degrades it.
QWEN3_SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
NUM_CTX = 16_384  # Ollama's default of 4,096 would cut the agent's prompts


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        local_model: LocalModel = QWEN3_8B,
        num_ctx: int = NUM_CTX,
        seed: int = 0,
        base_url: str = DEFAULT_URL,
        http: httpx2.Client | None = None,
    ) -> None:
        self.local_model = local_model
        self.model = local_model.name
        self.options = {"num_ctx": num_ctx, "seed": seed, **QWEN3_SAMPLING}
        self.http = http or httpx2.Client(base_url=base_url, timeout=600)
        self._checked = False

    def check_model(self) -> None:
        """Raise `InvalidRun` unless the local model is the pinned build."""
        tags = self._request("GET", "/api/tags")
        found = next((m for m in tags.get("models", []) if m.get("name") == self.model), None)
        if found is None:
            raise InvalidRun(f"{self.model} is not pulled (ollama pull {self.model})")
        quantization = found.get("details", {}).get("quantization_level")
        if found.get("digest") != self.local_model.digest or quantization != self.local_model.quantization:
            raise InvalidRun(f"{self.model} is {found.get('digest')} {quantization}, pinned {self.local_model}")
        self._checked = True

    def start(self, system: str, tools: Sequence[ToolSpec]) -> "OllamaConversation":
        if not self._checked:
            self.check_model()
        return OllamaConversation(self, system, tools)

    def unload(self) -> None:
        """Free the model's memory now instead of after Ollama's keep-alive."""
        self._request("POST", "/api/generate", {"model": self.model, "keep_alive": 0})

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, json=body)
        except httpx2.TransportError as exc:
            raise ProviderUnavailable(f"ollama: {exc}") from exc
        if response.status_code >= 500:
            raise ProviderUnavailable(f"ollama {path}: {response.status_code} {response.text}")
        if response.status_code >= 400:
            raise ProviderRequestError(f"ollama {path}: {response.status_code} {response.text}")
        return response.json()


class OllamaConversation:
    def __init__(self, provider: OllamaProvider, system: str, tools: Sequence[ToolSpec]) -> None:
        self._provider = provider
        self._tools = tuple(tools)
        self._tool_params = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
            }
            for t in self._tools
        ]
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self._closed = False

    def send_user(self, text: str) -> Turn:
        return self._send([{"role": "user", "content": text}])

    def send_tool_results(self, results: Sequence[ToolResult]) -> Turn:
        return self._send([{"role": "tool", "content": r.content, "tool_name": r.call.name} for r in results])

    def _send(self, messages: list[dict[str, Any]]) -> Turn:
        if self._closed:
            raise ConversationClosed("the previous turn ended the conversation")
        self._messages.extend(messages)
        provider = self._provider
        request = {
            "model": provider.model,
            "messages": self._messages,
            "tools": self._tool_params,
            "stream": False,
            "think": True,  # the sampling above is the model card's thinking-mode setting
            "truncate": False,
            "options": provider.options,
        }
        started = time.monotonic()
        try:
            response = provider.http.post("/api/chat", json=request)
        except httpx2.TransportError as exc:
            raise ProviderUnavailable(f"ollama: {exc}") from exc
        duration = time.monotonic() - started

        common = {"requested_model": provider.model, "cost_usd": 0.0, "duration_s": duration}
        if response.status_code == 400 and "exceed_context_size_error" in response.text:
            self._closed = True
            return Turn(
                stop=Stop.CONTEXT_LIMIT,
                text="",
                tool_calls=(),
                served_model=provider.model,
                usage=Usage(),
                context_limit_hit=True,
                **common,
            )
        if response.status_code >= 500:
            raise ProviderUnavailable(f"ollama /api/chat: {response.status_code} {response.text}")
        if response.status_code >= 400:
            raise ProviderRequestError(f"ollama /api/chat: {response.status_code} {response.text}")
        return self._turn(response.json(), common)

    def _turn(self, data: dict[str, Any], common: dict[str, Any]) -> Turn:
        provider = self._provider
        if data.get("model") != provider.model:
            raise InvalidRun(f"turn served by {data.get('model')}, configured {provider.model}")
        usage = Usage(input_tokens=data.get("prompt_eval_count", 0), output_tokens=data.get("eval_count", 0))
        full = usage.input_tokens + usage.output_tokens >= provider.options["num_ctx"]
        common |= {"served_model": data["model"], "usage": usage, "context_limit_hit": full}

        if data.get("done_reason") == "length":  # the context filled up while generating
            self._closed = True
            return Turn(stop=Stop.CONTEXT_LIMIT if full else Stop.MAX_TOKENS, text="", tool_calls=(), **common)

        message = data.get("message", {})
        raw_calls = message.get("tool_calls") or []
        # The model's thinking stays out of the history: the Qwen3 card keeps only the final output of past turns.
        self._messages.append({"role": "assistant", "content": message.get("content", ""), "tool_calls": raw_calls})
        text = message.get("content", "")
        calls = tuple(
            ToolCall(
                id=call.get("id") or f"call-{i}",
                name=call.get("function", {}).get("name", ""),
                arguments=call.get("function", {}).get("arguments"),
            )
            for i, call in enumerate(raw_calls)
        )
        malformed = sum(not tool_call_is_valid(self._tools, call.name, call.arguments) for call in calls)
        if not calls and "<tool_call>" in text:  # the template's call markup that Ollama could not parse
            malformed = 1
        if malformed:
            stop = Stop.MALFORMED_TOOL_CALL
            return Turn(stop=stop, text=text, tool_calls=(), malformed_tool_calls=malformed, **common)
        return Turn(stop=Stop.TOOL_USE if calls else Stop.END_TURN, text=text, tool_calls=calls, **common)
