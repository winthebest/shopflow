"""gateway service: the only public API (`GET /products`, `POST /checkout`, `GET /orders/{id}`).

It validates input at the edge, forwards to `orders` with a 1s total deadline (never retried: `POST /checkout` is not
idempotent), and turns downstream failures into explicit 502 (unreachable or broken) / 504 (too slow) responses.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings

from shopflow_common.log import AccessLogMiddleware, configure_logging
from shopflow_common.schemas import MAX_ID, CheckoutRequest
from shopflow_common.server import serve
from shopflow_common.telemetry import setup_telemetry

SERVICE = "gateway"
PORT = 8000
ORDERS_TIMEOUT_S = 1.0  # contract: gateway -> orders 1s

log = logging.getLogger(SERVICE)


class Settings(BaseSettings):
    orders_url: str = "http://localhost:8001"
    log_level: str = "INFO"


async def forward(request: Request, method: str, path: str, json: Any = None) -> Response:
    client: httpx.AsyncClient = request.app.state.orders
    try:
        async with asyncio.timeout(ORDERS_TIMEOUT_S):
            upstream = await client.request(method, path, json=json)
    except (TimeoutError, httpx.TimeoutException):
        log.warning("orders timed out", extra={"upstream_path": path})
        return JSONResponse({"detail": "orders service timed out"}, status_code=504)
    except httpx.TransportError as exc:
        log.warning("orders unreachable", extra={"upstream_path": path, "error": type(exc).__name__})
        return JSONResponse({"detail": "orders service unavailable"}, status_code=502)

    if upstream.status_code >= 500 and upstream.status_code not in (502, 503, 504):
        log.error("orders failed", extra={"upstream_path": path, "upstream_status": upstream.status_code})
        return JSONResponse(
            {"detail": "orders service error", "upstream_status": upstream.status_code}, status_code=502
        )
    # 2xx/4xx, and 502/503/504 that orders already mapped from its own dependency (payments), pass through as-is;
    # so does `Retry-After` (sent with 503 while the payments circuit is open).
    retry_after = upstream.headers.get("retry-after")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"Retry-After": retry_after} if retry_after else None,
    )


def create_app(settings: Settings | None = None, orders_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=settings.orders_url,
            timeout=ORDERS_TIMEOUT_S,
            # Drop idle connections before uvicorn's 5s keep-alive timeout closes them under us.
            limits=httpx.Limits(keepalive_expiry=2),
            transport=orders_transport,
        ) as client:
            app.state.orders = client
            yield

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    app.add_middleware(AccessLogMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        # No dependencies: when orders is down the gateway must stay in rotation and answer 502/504 itself
        # (with server spans for the SLI) instead of Envoy's "503 no healthy upstream".
        return {"status": "ok"}

    @app.get("/products")
    async def list_products(request: Request) -> Response:
        return await forward(request, "GET", "/products")

    @app.post("/checkout")
    async def checkout(body: CheckoutRequest, request: Request) -> Response:
        return await forward(request, "POST", "/orders", json=body.model_dump(mode="json"))

    @app.get("/orders/{order_id}")
    async def get_order(order_id: Annotated[int, Path(gt=0, le=MAX_ID)], request: Request) -> Response:
        return await forward(request, "GET", f"/orders/{order_id}")

    return app


def main() -> None:
    settings = Settings()
    configure_logging(SERVICE, settings.log_level)
    app = create_app(settings)
    setup_telemetry(app, SERVICE)
    serve(app, port=PORT)
