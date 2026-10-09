import asyncio
from decimal import Decimal

import httpx
import pytest

import orders.payments_client as payments_client
from orders.db import async_dsn
from orders.payments_client import ChargeOutcome, charge


async def charge_with(handler) -> ChargeOutcome:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://payments") as client:
        return await charge(client, 1, Decimal("9.90"))


async def test_201_is_succeeded_with_charge_id():
    outcome = await charge_with(lambda r: httpx.Response(201, json={"charge_id": "c-1", "status": "succeeded"}))
    assert outcome == ChargeOutcome("succeeded", charge_id="c-1")


async def test_402_is_declined():
    outcome = await charge_with(lambda r: httpx.Response(402, json={"charge_id": None, "status": "declined"}))
    assert outcome == ChargeOutcome("declined")


async def test_201_without_charge_id_is_error_unavailable():
    assert await charge_with(lambda r: httpx.Response(201, text="not json")) == ChargeOutcome(
        "error", error="unavailable"
    )
    assert await charge_with(lambda r: httpx.Response(201, json={})) == ChargeOutcome("error", error="unavailable")


async def test_protocol_error_is_error_unavailable():
    def handler(request):
        raise httpx.RemoteProtocolError("peer closed connection", request=request)

    assert await charge_with(handler) == ChargeOutcome("error", error="unavailable")


async def test_5xx_is_error_unavailable():
    assert await charge_with(lambda r: httpx.Response(503)) == ChargeOutcome("error", error="unavailable")


async def test_connect_error_is_error_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    assert await charge_with(handler) == ChargeOutcome("error", error="unavailable")


async def test_deadline_is_error_timeout(monkeypatch):
    monkeypatch.setattr(payments_client, "PAYMENTS_TIMEOUT_S", 0.05)

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(201, json={"charge_id": "late"})

    assert await charge_with(handler) == ChargeOutcome("error", error="timeout")


async def test_charge_sends_order_and_amount():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"charge_id": "c-2"})

    await charge_with(handler)
    assert seen[0].url.path == "/charges"
    assert seen[0].read() == b'{"order_id":1,"amount":"9.90"}'


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@h:5432/shop", "postgresql+asyncpg://u:p@h:5432/shop"),
        ("postgres://u:p@h/shop", "postgresql+asyncpg://u:p@h/shop"),
        ("postgresql+asyncpg://u:p@h/shop", "postgresql+asyncpg://u:p@h/shop"),
        ("postgresql://u:p@h/shop?sslmode=require", "postgresql+asyncpg://u:p@h/shop?ssl=require"),
        ("postgresql://u:p%40ss@h/shop", "postgresql+asyncpg://u:p%40ss@h/shop"),
    ],
)
def test_async_dsn(url, expected):
    assert async_dsn(url) == expected
