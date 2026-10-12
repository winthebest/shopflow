"""fulfillment-worker: consumes `shop.public.orders`, creates one shipment per paid order.

Runs the consume loop as a background task next to a small HTTP server (port 8003) for probes:
`/healthz` fails once the loop has died (so Kubernetes restarts it from the last committed offset),
`/readyz` also checks the database. SIGTERM: uvicorn stops, the lifespan lets the batch in progress finish and commit,
then the consumer leaves its group so the remaining replicas take over its partitions at once.
"""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from aiokafka import AIOKafkaConsumer
from aiokafka.helpers import create_ssl_context
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from sqlalchemy import text

from fulfillment_worker.settings import Settings
from fulfillment_worker.store import create_shipments
from fulfillment_worker.worker import Worker
from shopflow_common.db import create_engine
from shopflow_common.log import AccessLogMiddleware, configure_logging
from shopflow_common.server import GRACEFUL_SHUTDOWN_S, serve
from shopflow_common.telemetry import setup_telemetry

SERVICE = "fulfillment-worker"
PORT = 8003
READY_TIMEOUT_S = 1.0
STOP_TIMEOUT_S = GRACEFUL_SHUTDOWN_S - 2  # leave time to close the consumer and the engine
# Liveness: the loop polls every second when idle; a batch takes at most batch x latency (<= 60s, enforced by
# settings.MAX_BATCH_LATENCY_MS) plus ~25s of DB retries.
STALLED_AFTER_S = 120.0
DB_CONNECT_TIMEOUT_S = 5.0  # no 1s request budget here, unlike orders
DB_STATEMENT_TIMEOUT_S = 10.0

log = logging.getLogger("fulfillment_worker")


def kafka_consumer(settings: Settings) -> AIOKafkaConsumer:
    security: dict = {"security_protocol": settings.kafka_security_protocol}
    if settings.kafka_security_protocol == "SASL_SSL":
        security |= {
            "sasl_mechanism": "SCRAM-SHA-512",
            "sasl_plain_username": settings.kafka_username,
            "sasl_plain_password": settings.kafka_password.get_secret_value() if settings.kafka_password else None,
            "ssl_context": create_ssl_context(cafile=settings.kafka_ca_file),
        }
    return AIOKafkaConsumer(
        settings.kafka_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_group_id,
        client_id=f"{SERVICE}-{socket.gethostname()}",
        enable_auto_commit=False,  # offsets are committed after the database transaction (worker.py)
        auto_offset_reset="earliest",  # a new group replays the topic; shipments are idempotent
        **security,
    )


def create_app(settings: Settings | None = None, consumer: AIOKafkaConsumer | None = None) -> FastAPI:
    settings = settings or Settings()  # KAFKA_* and DATABASE_URL come from the environment

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(
            settings.dsn(),
            connect_timeout_s=DB_CONNECT_TIMEOUT_S,
            command_timeout_s=DB_STATEMENT_TIMEOUT_S,
            # One batch at a time plus the readiness check: it held 2 per replica under load (ops slot). Small, so
            # KEDA's maximum replicas fit shop-db's connection budget.
            pool_size=2,
            max_overflow=0,
        )
        kafka = consumer or kafka_consumer(settings)
        await kafka.start()
        worker = Worker(
            kafka,
            lambda changes: create_shipments(engine, changes),
            max_records=settings.batch_max_records,
            shipment_latency_s=settings.shipment_latency_ms / 1000,
        )
        task = asyncio.create_task(worker.run(), name="consume")
        app.state.engine, app.state.task, app.state.worker = engine, task, worker
        try:
            yield
        finally:
            worker.stop()
            with suppress(Exception):  # Worker.run() logged the crash
                await asyncio.wait_for(task, STOP_TIMEOUT_S)
            await kafka.stop()  # leave the group: partitions move to the other replicas without waiting
            await engine.dispose()

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    app.add_middleware(AccessLogMiddleware)

    def loop_failure(request: Request) -> str | None:
        task: asyncio.Task = request.app.state.task
        if not task.done():
            stalled_s = request.app.state.worker.seconds_since_poll()
            return f"consume loop stalled for {stalled_s:.0f}s" if stalled_s > STALLED_AFTER_S else None
        exc = task.exception() if not task.cancelled() else None
        return f"consume loop stopped: {exc!r}" if exc else "consume loop stopped"

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        failure = loop_failure(request)
        if failure:
            return JSONResponse({"status": failure}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        failure = loop_failure(request)
        if failure:
            return JSONResponse({"status": failure}, status_code=503)
        try:
            async with asyncio.timeout(READY_TIMEOUT_S), request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # any failure means "not ready"
            log.warning("database not ready", extra={"error": type(exc).__name__})
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


def main() -> None:
    settings = Settings()
    configure_logging(SERVICE, settings.log_level)
    app = create_app(settings)
    if setup_telemetry(app, SERVICE) is not None:
        AsyncPGInstrumentor().instrument()
    serve(app, port=PORT)
