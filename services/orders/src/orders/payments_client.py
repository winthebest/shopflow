"""Call the payments provider with an 800ms total deadline and classify the answer."""

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import httpx

PAYMENTS_TIMEOUT_S = 0.8  # contract: orders -> payments 800ms


@dataclass(frozen=True)
class ChargeOutcome:
    status: Literal["succeeded", "declined", "error"]
    charge_id: str | None = None
    error: Literal["timeout", "unavailable"] | None = None


async def charge(client: httpx.AsyncClient, order_id: int, amount: Decimal) -> ChargeOutcome:
    try:
        async with asyncio.timeout(PAYMENTS_TIMEOUT_S):
            response = await client.post("/charges", json={"order_id": order_id, "amount": str(amount)})
    except (TimeoutError, httpx.TimeoutException):
        return ChargeOutcome("error", error="timeout")
    except httpx.TransportError:
        return ChargeOutcome("error", error="unavailable")

    if response.status_code == 201:
        return ChargeOutcome("succeeded", charge_id=response.json()["charge_id"])
    if response.status_code == 402:
        return ChargeOutcome("declined")
    return ChargeOutcome("error", error="unavailable")
