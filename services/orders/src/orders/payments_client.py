"""Call the payments provider with an 800ms total deadline and classify the answer. Never raises."""

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
    except httpx.HTTPError:
        return ChargeOutcome("error", error="unavailable")

    if response.status_code == 201:
        try:
            return ChargeOutcome("succeeded", charge_id=str(response.json()["charge_id"]))
        except (ValueError, KeyError, TypeError):  # answered, but not in the agreed shape: treat as no answer
            return ChargeOutcome("error", error="unavailable")
    if response.status_code == 402:
        return ChargeOutcome("declined")
    return ChargeOutcome("error", error="unavailable")
