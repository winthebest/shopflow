"""POST /orders while the payments circuit is not closed. The database is unreachable on purpose: these paths must
answer before touching it, or hand the breaker's probe back when the checkout dies before charging."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from orders.main import create_app
from orders.settings import Settings

CHECKOUT = {"customer_id": 1, "items": [{"product_id": 2, "quantity": 1}]}
UNREACHABLE_DB = "postgresql://shop_app:x@127.0.0.1:9/shop"


@asynccontextmanager
async def orders_app(**settings):
    app = create_app(
        Settings(database_url=UNREACHABLE_DB, payments_url="http://payments", payments_breaker_min_calls=1, **settings),
        payments_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://orders") as client,
    ):
        payments = app.state.payments
        payments.breaker.record(payments.admit(), failed=True)  # one failure opens it (min_calls=1)
        yield client, payments


async def test_open_circuit_answers_503_with_retry_after_before_creating_an_order():
    async with orders_app() as (client, _):
        response = await client.post("/orders", json=CHECKOUT)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": "payments unavailable (circuit open)"}


async def test_checkout_failing_before_the_charge_releases_the_half_open_probe():
    async with orders_app(payments_breaker_open_s=0.05) as (client, payments):
        await asyncio.sleep(0.06)  # half-open: this checkout takes the probe, then the database is unreachable
        with pytest.raises(OSError):
            await client.post("/orders", json=CHECKOUT)
        probe = payments.admit()
    assert probe is not None and probe.probe  # the slot was handed back, the next checkout may probe
