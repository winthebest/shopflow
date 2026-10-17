"""Record and replay LLM calls, so CI can run the scoring pipeline with no API call and no key.

A `Cassette` is an httpx2 transport: give it to the provider's HTTP client (`anthropic.DefaultHttpxClient(transport=
...)`, or the `httpx2.Client` of the Ollama provider). Recording passes each request to the real backend and stores
the exchange; replaying returns the stored response. A request that was never recorded gets a 404 `cassette_miss`
error, which no client retries and both providers raise as `ProviderRequestError`, and is listed in `misses`: a changed
prompt or tool cannot pass on an old answer. (Raising inside the transport would not do: the Anthropic SDK turns any
transport exception into a retried connection error.) Identical requests (repetitions of one prompt) are replayed
in the order they were recorded.

The key is a SHA-256 of the method, the path, the allowed headers and the JSON body after `scrub`: volatile values the
harness generates (a per-run namespace, say) replaced by stable placeholders. Before anything is stored or hashed,
`redact` removes what must not reach git: every header outside `KEPT_REQUEST_HEADERS`/`KEPT_RESPONSE_HEADERS` (so no
API key or Authorization), user info and query strings in the URL, and in bodies: credentials in URLs, Basic and
Bearer values of an Authorization header, bare Bearer tokens, `X-Trino-*` header lines, Discord webhook URLs, Anthropic
keys, AWS key ids and secret keys, and PEM private keys.

Only LLM calls are recorded; Trino and the executor are never replayed. Held-out cassettes go under
evals/cassettes/holdout/, which git ignores.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import httpx2

KEPT_REQUEST_HEADERS = ("anthropic-beta", "anthropic-version", "content-type")
KEPT_RESPONSE_HEADERS = ("content-type",)
REDACTIONS = (
    # Credentials a prompt or a tool result could carry: a key in a file the agent read, a config it described.
    # No \b anchors: in a JSON body a value that starts a line follows the escaped newline `\n`, a word character.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "<private-key>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "<anthropic-key>"),
    (re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), "<aws-key-id>"),
    (
        re.compile(r"(?i)(aws_secret_access_key|aws_session_token)(\s*[=:]\s*(?:\\?[\"'])?)[A-Za-z0-9/+=]{16,}"),
        r"\1\2<redacted>",
    ),
    (re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/[^\s\"'\\]+"), "<discord-webhook>"),
    (re.compile(r"(https?://)[^/\s:@\"'\\]+:[^/\s@\"'\\]+@"), r"\1<credentials>@"),
    # Basic and Bearer only in their header form, or a bare Bearer value that looks like a token (20+ characters,
    # a digit among them): the words alone are ordinary prose ("a Basic understanding"), and responses replay as stored.
    (re.compile(r"(?i)(authorization\s*:\s*(?:Basic|Bearer)\s+)[A-Za-z0-9+/=._~-]{8,}"), r"\1<credentials>"),
    (re.compile(r"(Bearer\s+)(?=[A-Za-z0-9+/=._~-]*[0-9])[A-Za-z0-9+/=._~-]{20,}"), r"\1<credentials>"),
    (re.compile(r"(?i)(X-Trino-[A-Za-z-]+)\s*:\s*[^\r\n\"\\]+"), r"\1: <redacted>"),
)


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class Cassette(httpx2.BaseTransport):
    def __init__(
        self,
        path: Path,
        mode: Literal["record", "replay"],
        *,
        backend: httpx2.BaseTransport | None = None,
        scrub: Sequence[tuple[str, str]] = (),
    ) -> None:
        if mode == "record" and backend is None:
            backend = httpx2.HTTPTransport()
        self.path = path
        self.mode = mode
        self.backend = backend
        self.scrub = [(re.compile(pattern), placeholder) for pattern, placeholder in scrub]
        self.interactions: dict[str, list[dict[str, Any]]] = {}
        if mode == "replay":
            self.interactions = json.loads(path.read_text())["interactions"]
        self._served: dict[str, int] = {}
        self.misses: list[str] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        stored_request = self._stored_request(request)
        key = hashlib.sha256(json.dumps(stored_request, sort_keys=True).encode()).hexdigest()
        if self.mode == "replay":
            return self._replay(key, stored_request)

        response = self.backend.handle_request(request)
        body = response.read()
        headers = {k: v for k, v in response.headers.items() if k.lower() in KEPT_RESPONSE_HEADERS}
        stored_response = {"status": response.status_code, "headers": headers, "body": redact(body.decode())}
        self.interactions.setdefault(key, []).append({"request": stored_request, "response": stored_response})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 1, "interactions": self.interactions}, indent=1, sort_keys=True))
        return httpx2.Response(response.status_code, headers=response.headers, content=body, request=request)

    def _stored_request(self, request: httpx2.Request) -> dict[str, Any]:
        body = redact(request.content.decode())
        for pattern, placeholder in self.scrub:
            body = pattern.sub(placeholder, body)
        try:
            parsed: Any = json.loads(body)
        except json.JSONDecodeError:
            parsed = body
        headers = {k: v for k, v in request.headers.items() if k.lower() in KEPT_REQUEST_HEADERS}
        return {"method": request.method, "path": request.url.path, "headers": headers, "body": parsed}

    def _replay(self, key: str, stored_request: dict[str, Any]) -> httpx2.Response:
        recordings = self.interactions.get(key, [])
        index = self._served.get(key, 0)
        if index >= len(recordings):
            miss = (
                f"{stored_request['method']} {stored_request['path']}: no recording left for this request in "
                f"{self.path} (recorded {len(recordings)}); re-record it if the change is intended"
            )
            self.misses.append(miss)
            return httpx2.Response(404, json={"type": "error", "error": {"type": "cassette_miss", "message": miss}})
        self._served[key] = index + 1
        stored = recordings[index]["response"]
        return httpx2.Response(stored["status"], headers=stored["headers"], content=stored["body"].encode())
