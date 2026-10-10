"""orders service: `GET /products`, `POST /orders`, `GET /orders/{id}` on the shop database.

Checkout = transaction 1 (order + items, `pending`) -> payments call (800ms deadline, retries and circuit breaker in
`payments_client`) -> transaction 2 (payment row, order `paid | failed`, idempotent). No answer from payments does not
mean no charge: the order stays `pending`, checkout answers 504 / 502 so the caller sees the dependency failure, and
the stranded-order sweeper (`sweeper`) charges again (payments answers the same for the same order) and settles. While
the circuit is open, checkout answers 503 + `Retry-After` before creating anything. The answer is built from what the
two transactions returned: a read after settling could fail and lose the acknowledgement of a paid order.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from orders.db import create_engine
from orders.payments_client import PAYMENTS_TIMEOUT_S, PaymentsClient
from orders.repository import (
    InvalidCheckoutError,
    OrderOut,
    PendingOrder,
    ProductOut,
    create_pending_order,
    get_order,
    list_products,
    settle_order,
)
from orders.settings import Settings
from orders.sweeper import Sweeper
from shopflow_common.log import AccessLogMiddleware, configure_logging
from shopflow_common.schemas import MAX_ID, CheckoutRequest
from shopflow_common.server import serve
from shopflow_common.telemetry import setup_telemetry

SERVICE = "orders"
PORT = 8001
READY_TIMEOUT_S = 1.0

log = logging.getLogger(SERVICE)


def create_app(settings: Settings | None = None, payments_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()  # DATABASE_URL comes from the environment

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url.get_secret_value())
        try:
            payments_http = {
                "base_url": settings.payments_url,
                "timeout": PAYMENTS_TIMEOUT_S,
                "transport": payments_transport,
            }
            async with (
                # Drop idle connections before uvicorn's 5s keep-alive timeout closes them under us.
                httpx.AsyncClient(limits=httpx.Limits(keepalive_expiry=2), **payments_http) as payments,
                # Retries: a new connection every time, so kube-proxy may pick another pod (payments_client).
                httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0), **payments_http) as retries,
            ):
                app.state.engine = engine
                app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                app.state.payments = PaymentsClient(
                    payments, settings.retry_policy(), settings.breaker_config(), retry_http=retries
                )
                sweeper = None
                if settings.sweep_interval_s > 0:
                    sweeper = asyncio.create_task(
                        Sweeper(
                            app.state.sessionmaker,
                            app.state.payments,
                            interval_s=settings.sweep_interval_s,
                            stale_after_s=settings.sweep_stale_after_s,
                            batch=settings.sweep_batch,
                        ).run(),
                        name="stranded-order-sweeper",
                    )
                try:
                    yield
                finally:
                    if sweeper is not None:
                        sweeper.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await sweeper
        finally:
            await engine.dispose()

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    app.add_middleware(AccessLogMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        engine: AsyncEngine = request.app.state.engine
        try:
            async with asyncio.timeout(READY_TIMEOUT_S), engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # any failure means "not ready"
            log.warning("database not ready", extra={"error": type(exc).__name__})
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/products")
    async def products(request: Request) -> list[ProductOut]:
        async with request.app.state.sessionmaker() as session:
            return await list_products(session)

    @app.post("/orders", status_code=201, response_model=OrderOut)
    async def create_order(body: CheckoutRequest, request: Request) -> Any:
        sessionmaker = request.app.state.sessionmaker
        payments: PaymentsClient = request.app.state.payments
        permit = payments.admit()
        if permit is None:  # circuit open: refuse before creating an order that could only fail
            return JSONResponse(
                {"detail": "payments unavailable (circuit open)"},
                status_code=503,
                headers={"Retry-After": str(payments.breaker.retry_after_s())},
            )
        try:
            pending: PendingOrder = await create_pending_order(sessionmaker, body)
        except InvalidCheckoutError as exc:
            payments.release(permit)
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except BaseException:
            payments.release(permit)
            raise

        outcome = await payments.charge(pending.id, pending.total, permit)
        if outcome.error is not None:
            # A timeout may come after payments charged: leave the order `pending` for the sweeper to settle with the
            # real answer, instead of recording `failed` for a charge that may have happened.
            log.warning("payments did not answer, order left pending", extra={"order_id": pending.id})
            code, detail = (504, "payments timed out") if outcome.error == "timeout" else (502, "payments unavailable")
            return JSONResponse({"detail": detail, "order_id": pending.id, "status": "pending"}, status_code=code)
        try:
            settled = await settle_order(sessionmaker, pending.id, pending.total, outcome)
        except Exception:
            # The order stays `pending` and the provider outcome lives only in this line: reconcile from it.
            log.exception(
                "settle failed, order left pending",
                extra={"order_id": pending.id, "payment_status": outcome.status, "charge_id": outcome.charge_id},
            )
            raise
        log.info(
            "order settled" if settled.settled_now else "order already settled",
            extra={"order_id": pending.id, "order_status": settled.status, "payment_status": outcome.status},
        )
        return OrderOut(
            id=pending.id,
            customer_id=pending.customer_id,
            status=settled.status,
            total=pending.total,
            created_at=pending.created_at,
            updated_at=settled.updated_at,
            items=pending.items,
        )

    @app.get("/orders/{order_id}", response_model=OrderOut)
    async def read_order(order_id: Annotated[int, Path(gt=0, le=MAX_ID)], request: Request) -> Any:
        async with request.app.state.sessionmaker() as session:
            order = await get_order(session, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail=f"order {order_id} not found")
        return order

    return app


def main() -> None:
    settings = Settings()
    configure_logging(SERVICE, settings.log_level)
    app = create_app(settings)
    if setup_telemetry(app, SERVICE) is not None:
        AsyncPGInstrumentor().instrument()
    serve(app, port=PORT)
