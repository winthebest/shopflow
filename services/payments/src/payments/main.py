"""payments service: a mock provider. Each charge waits PAYMENT_LATENCY_MS, then is declined (HTTP 402) or succeeds
(HTTP 201).

Idempotent per `order_id`, like a real provider's idempotency key, so orders may retry a charge whose answer it never
got (docs/contracts/services.md, ADR 0102): the outcome and `charge_id` are derived from the order id, not drawn at
random, so every replica gives the same answer to every attempt. A fixed PAYMENT_FAILURE_RATE of the order ids,
spread uniformly by a hash, is declined.
"""

import asyncio
import hashlib
import logging
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5

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

CHARGE_NAMESPACE = UUID("6f1d2c4e-8a51-4c0f-9a57-2f0f5a3e9b10")  # fixed: charge ids must not change between releases


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


def charge_id_for(order_id: int) -> UUID:
    return uuid5(CHARGE_NAMESPACE, f"order-{order_id}")


def declined(order_id: int, failure_rate: float) -> bool:
    """Whether this order's charge is declined: the order id hashed to a uniform number in [0, 1)."""
    digest = hashlib.sha256(f"order-{order_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < failure_rate


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
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
        if declined(req.order_id, settings.payment_failure_rate):
            response.status_code = 402
            log.info("charge declined", extra={"order_id": req.order_id})
            return ChargeResult(charge_id=None, status="declined")
        return ChargeResult(charge_id=charge_id_for(req.order_id), status="succeeded")

    return app


def main() -> None:
    settings = Settings()
    configure_logging(SERVICE, settings.log_level)
    app = create_app(settings)
    setup_telemetry(app, SERVICE)
    serve(app, port=PORT)
