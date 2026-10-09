"""JSON-lines logging on stdout, correlated with the active OpenTelemetry span.

Keys follow docs/contracts/services.md (Logs): `timestamp` (RFC 3339 UTC), `level`, `message`, `service`,
`trace_id`, `span_id` (lowercase hex, empty string when no span is active, e.g. while the SDK is disabled), plus
any `extra=` fields. Loki uses `trace_id` to jump to the matching trace in Tempo once Phase 3 turns tracing on.
"""

import json
import logging
import sys
import time
from datetime import UTC, datetime

from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Attributes every LogRecord has; anything else on the record came from `extra=` and is emitted as a field.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}

HEALTH_PATHS = frozenset({"/healthz", "/readyz"})

# The contract allows DEBUG|INFO|WARNING|ERROR; Python's CRITICAL is reported as ERROR.
_LEVELS = {"CRITICAL": "ERROR"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        entry = {
            "timestamp": timestamp.replace("+00:00", "Z"),
            "level": _LEVELS.get(record.levelname, record.levelname),
            "message": record.getMessage(),
            "service": self.service,
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else "",
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else "",
            "logger": record.name,
        }
        entry.update(
            (key, value)
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    """Route every logger (including uvicorn's) through one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


class AccessLogMiddleware:
    """One JSON line per request (health probes skipped).

    Pure ASGI rather than BaseHTTPMiddleware: no extra task per request, and it runs inside the OTel middleware
    so the request span is current when the line is written.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.log = logging.getLogger("access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in HEALTH_PATHS:
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            self.log.info(
                "request",
                extra={
                    "http_method": scope["method"],
                    "http_path": scope["path"],
                    "http_status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
