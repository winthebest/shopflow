"""payments service: a mock provider. Each charge waits PAYMENT_LATENCY_MS, then is declined with
probability PAYMENT_FAILURE_RATE (HTTP 402) or succeeds (HTTP 201)."""

import asyncio
import logging
import random
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings

from shopflow_common.log import AccessLogMiddleware, configure_logging
from shopflow_common.schemas import MAX_ID
from shopflow_common.server import serve
from shopflow_common.telemetry import setup_telemetry

SERVICE = "payments"
PORT = 8002

log = logging.getLogger(SERVICE)


class Settings(BaseSettings):
    payment_latency_ms: int = Field(default=50, ge=0, le=60_000)
    payment_failure_rate: float = Field(default=0.02, ge=0.0, le=1.0)
    log_level: str = "INFO"


class ChargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(gt=0, le=MAX_ID)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class ChargeResult(BaseModel):
    charge_id: UUID | None
    status: Literal["succeeded", "declined"]


def create_app(settings: Settings | None = None, rng: random.Random | None = None) -> FastAPI:
    settings = settings or Settings()
    rng = rng or random.Random()  # noqa: S311 - simulated failures, not security
    app = FastAPI(title=SERVICE)
    app.add_middleware(AccessLogMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ok"}  # no dependencies

    @app.post("/charges", status_code=201)
    async def charge(req: ChargeRequest, response: Response) -> ChargeResult:
        await asyncio.sleep(settings.payment_latency_ms / 1000)
        if rng.random() < settings.payment_failure_rate:
            response.status_code = 402
            log.info("charge declined", extra={"order_id": req.order_id})
            return ChargeResult(charge_id=None, status="declined")
        return ChargeResult(charge_id=uuid4(), status="succeeded")

    return app


def main() -> None:
    settings = Settings()
    configure_logging(SERVICE, settings.log_level)
    app = create_app(settings)
    setup_telemetry(app, SERVICE)
    serve(app, port=PORT)
