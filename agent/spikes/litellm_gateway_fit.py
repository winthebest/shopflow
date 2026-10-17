# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["litellm[proxy]==1.104.0", "anthropic==1.15.0"]
# ///
"""Gateway spike: does a LiteLLM proxy pass Claude Opus 5.5 requests through intact (docs/adr/0105-llm-gateway.md)?

The proxy runs as a local process in front of agent/tests/fake_anthropic.py, served on 127.0.0.1, and is stopped at
the end. Nothing can reach the real API: every upstream is an explicit 127.0.0.1 address, any other HTTP(S) egress
goes to a dead proxy, and LiteLLM reads its bundled price map instead of fetching it. Three routes are checked: the
pass-through endpoint forwarding the client's headers, the pass-through endpoint with its own upstream key, and the
unified /v1/messages endpoint routed through `model_list`. Dependencies are hash-locked in litellm_gateway_fit.py.lock.

    uv run --locked --script agent/spikes/litellm_gateway_fit.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic
import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import fake_anthropic as fa  # the fake lives with the agent's tests

MASTER_KEY = "sk-spike-local-only"  # the proxy's own key, valid only for this local process
UPSTREAM_KEY = "upstream-fake-key"
TOOL = {
    "name": "row_count",
    "description": "Number of rows in a table of the read-only catalog.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"table": {"type": "string"}},
        "required": ["table"],
        "additionalProperties": False,
    },
}
REQUEST = {
    "model": "claude-opus-5-5",
    "max_tokens": 4096,
    "system": [{"type": "text", "text": "You operate a data lakehouse.", "cache_control": {"type": "ephemeral"}}],
    "tools": [TOOL],
    "tool_choice": {"type": "auto"},
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "high"},
    "cache_control": {"type": "ephemeral"},
    "messages": [{"role": "user", "content": "How many rows does lab_ro.shop.orders have?"}],
}
NO_CACHE = {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
FALLBACK_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 300,
    "iterations": [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1200, "output_tokens": 0} | NO_CACHE,
        {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1200, "output_tokens": 300} | NO_CACHE,
    ],
}
ROUTES = {"pass-through, client headers": "/claude", "pass-through, own key": "/claude-keyed", "unified": ""}
current: dict[str, fa.FakeMessagesAPI] = {}


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        request = httpx2.Request("POST", f"http://upstream{self.path}", headers=dict(self.headers), content=body)
        response = current["fake"]._handle(request)
        content = response.read()
        self.send_response(response.status_code)
        for name, value in response.headers.items():
            if name.lower() not in ("content-length", "transfer-encoding"):
                self.send_header(name, value)
        self.send_header("content-length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_proxy(upstream: str, workdir: Path) -> tuple[subprocess.Popen, int]:
    port = free_port()
    (workdir / "config.yaml").write_text(
        f"""model_list:
  - model_name: claude-opus-5-5
    litellm_params: {{model: anthropic/claude-opus-5-5, api_base: "{upstream}", api_key: {UPSTREAM_KEY}}}
general_settings:
  master_key: {MASTER_KEY}
  pass_through_endpoints:
    - {{path: /claude, target: "{upstream}", include_subpath: true, forward_headers: true}}
    - path: /claude-keyed
      target: "{upstream}"
      include_subpath: true
      headers: {{x-api-key: {UPSTREAM_KEY}, anthropic-version: "2023-06-01"}}
"""
    )
    dead = "http://127.0.0.1:9"  # nothing listens there: any egress the config did not name fails
    env = os.environ | {
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "ANTHROPIC_API_BASE": upstream,
        "ANTHROPIC_API_KEY": UPSTREAM_KEY,
        "HTTP_PROXY": dead,
        "HTTPS_PROXY": dead,
        "ALL_PROXY": dead,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    log = (workdir / "litellm.log").open("w")
    command = [str(Path(sys.executable).parent / "litellm"), "--config", str(workdir / "config.yaml")]
    proxy = subprocess.Popen([*command, "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=log, stderr=log)  # noqa: S603
    for _ in range(120):
        try:
            if httpx2.get(f"http://127.0.0.1:{port}/health/liveliness", timeout=1).status_code == 200:
                return proxy, port
        except httpx2.HTTPError:
            pass
        time.sleep(1)
    proxy.terminate()
    raise SystemExit(f"the proxy did not start; see {workdir / 'litellm.log'}")


def stream(client: anthropic.Anthropic, **extra: object) -> tuple[object, dict, httpx2.Headers]:
    """Final message, the body the client sent, and the response headers."""
    with client.beta.messages.stream(**REQUEST, **extra) as s:
        message = s.get_final_message()
        return message, json.loads(s.response.request.content), s.response.headers


def check_route(name: str, base_url: str) -> list[tuple[str, str, str]]:
    client = anthropic.Anthropic(api_key=MASTER_KEY, base_url=base_url, max_retries=0)
    rows = []

    current["fake"] = fake = fa.FakeMessagesAPI(fa.Reply([fa.thinking(), fa.text("ok")]))
    message, sent, _ = stream(client)
    received = fake.requests[-1]
    same = json.dumps(received.body, sort_keys=True) == json.dumps(sent, sort_keys=True)
    key = "the configured key" if received.headers.get("x-api-key") == UPSTREAM_KEY else "the client's proxy key"
    rows.append((name, "eval request reaches the upstream unchanged", f"{same}; upstream key: {key}"))

    reply = fa.Reply(
        [fa.fallback("claude-opus-5-5", "claude-opus-4-8"), fa.thinking(), fa.text("ok")],
        model="claude-opus-4-8",
        usage=FALLBACK_USAGE,
    )
    current["fake"] = fake = fa.FakeMessagesAPI(reply)
    message, sent, headers = stream(client, fallbacks="default", betas=["server-side-fallback-2026-07-01"])
    received = fake.requests[-1]
    rows.append(
        (
            name,
            "demo: `fallbacks` and its beta reach the upstream",
            f"client sent fallbacks={sent.get('fallbacks')!r}, "
            f"upstream got fallbacks={received.body.get('fallbacks')!r}, beta={received.headers.get('anthropic-beta')}",
        )
    )
    iterations = [i.type for i in message.usage.iterations or []]
    rows.append(
        (
            name,
            "demo: served model, `fallback` block, usage.iterations come back",
            f"model {message.model}, blocks {[b.type for b in message.content]}, iterations {iterations}; "
            f"cost header {headers.get('x-litellm-response-cost')}",
        )
    )

    details = {"type": "refusal", "category": "bio", "explanation": None}
    current["fake"] = fa.FakeMessagesAPI(fa.Reply([], stop_reason="refusal", stop_details=details))
    message, _, _ = stream(client)
    category = message.stop_details.category if message.stop_details else None
    rows.append((name, "refusal and stop_details come back", f"{message.stop_reason}, category {category}"))

    current["fake"] = fake = fa.FakeMessagesAPI(
        fa.Reply([fa.thinking(), fa.tool_use("toolu_1", "row_count", {"table": "x"})], stop_reason="tool_use"),
        fa.Reply([fa.thinking(), fa.text("1234 rows.")]),
    )
    first, _, _ = stream(client)
    answer = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "1234"}]}
    history = [*REQUEST["messages"], {"role": "assistant", "content": first.content}, answer]
    with client.beta.messages.stream(**{**REQUEST, "messages": history}) as s:
        final = s.get_final_message()
    rows.append((name, "tool round trip keeps the history append-only", f"{[b.type for b in final.content]}"))
    return rows


def main() -> None:
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    workdir = Path(tempfile.mkdtemp(prefix="litellm-spike-"))
    proxy, port = start_proxy(f"http://127.0.0.1:{upstream.server_address[1]}", workdir)
    rows = []
    try:
        for name, path in ROUTES.items():
            rows.extend(check_route(name, f"http://127.0.0.1:{port}{path}"))
    finally:
        proxy.terminate()
        proxy.wait(timeout=30)
        upstream.shutdown()
    width = max(len(check) for _, check, _ in rows)
    for route, check, result in rows:
        print(f"{route:<29} {check:<{width}}  {result}")
    print(f"proxy log: {workdir / 'litellm.log'}")


if __name__ == "__main__":
    main()
