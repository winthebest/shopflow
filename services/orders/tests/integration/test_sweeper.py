"""Stranded-order sweeper on real Postgres with the payments app: claims never overlap, the real answer is settled once.

`stale_after_s=0` makes any `pending` order committed earlier eligible; `backdate` ages orders for the lease tests
(the `updated_at` trigger is disabled for that one statement).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import orders.main
from orders.db import create_engine
from orders.payments_client import ChargeOutcome, PaymentsClient
from orders.repository import claim_stranded_orders, create_pending_order, oldest_pending_age_s, settle_order
from orders.resilience import BreakerConfig
from orders.sweeper import Sweeper
from payments.main import Settings as PaymentsSettings
from payments.main import charge_id_for
from payments.main import create_app as create_payments
from shopflow_common.schemas import CheckoutRequest

pytestmark = pytest.mark.integration

CHECKOUT = CheckoutRequest(customer_id=1, items=[{"product_id": 1, "quantity": 2}])
GATEWAY_CHECKOUT = {"customer_id": 1, "items": [{"product_id": 1, "quantity": 2}]}


def meter():
    return MeterProvider().get_meter("test")


@pytest.fixture
async def sessionmaker(seeded_db):
    engine = create_engine(seeded_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@asynccontextmanager
async def payments(failure_rate: float = 0.0, breaker: BreakerConfig | None = None) -> AsyncIterator[PaymentsClient]:
    app = create_payments(PaymentsSettings(payment_latency_ms=0, payment_failure_rate=failure_rate))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://payments") as http:
        yield PaymentsClient(http, breaker=breaker, meter=meter())


async def query(sessionmaker, sql: str, **params) -> list[tuple]:
    async with sessionmaker() as session:
        return [tuple(row) for row in await session.execute(text(sql), params)]


async def backdate(sessionmaker, seconds: int) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(text("ALTER TABLE orders DISABLE TRIGGER trg_orders_updated_at"))
        await session.execute(
            text("UPDATE orders SET updated_at = now() - make_interval(secs => :s) WHERE status = 'pending'"),
            {"s": seconds},
        )
        await session.execute(text("ALTER TABLE orders ENABLE TRIGGER trg_orders_updated_at"))


async def test_stranded_order_is_settled_with_the_answer_payments_gives_for_it(sessionmaker):
    pending = await create_pending_order(sessionmaker, CHECKOUT)  # charged or not, never settled
    async with payments() as client:
        assert await Sweeper(sessionmaker, client, stale_after_s=0, meter=meter()).sweep_once() == 1

    rows = await query(
        sessionmaker,
        "SELECT o.status, p.status, p.provider_ref FROM orders o JOIN payments p ON p.order_id = o.id WHERE o.id = :id",
        id=pending.id,
    )
    assert rows == [("paid", "succeeded", str(charge_id_for(pending.id)))]


async def test_declined_on_the_new_charge_settles_failed(sessionmaker):
    pending = await create_pending_order(sessionmaker, CHECKOUT)
    async with payments(failure_rate=1.0) as client:
        await Sweeper(sessionmaker, client, stale_after_s=0, meter=meter()).sweep_once()
    assert await query(sessionmaker, "SELECT status FROM orders WHERE id = :id", id=pending.id) == [("failed",)]


async def test_order_younger_than_the_threshold_is_left_to_its_request(sessionmaker):
    pending = await create_pending_order(sessionmaker, CHECKOUT)
    async with payments() as client:
        assert await Sweeper(sessionmaker, client, stale_after_s=30, meter=meter()).sweep_once() == 0
    assert await query(sessionmaker, "SELECT status FROM orders WHERE id = :id", id=pending.id) == [("pending",)]
    assert 0 <= await oldest_pending_age_s(sessionmaker) < 30


async def test_concurrent_claims_never_overlap_and_a_claim_leases_the_order(sessionmaker):
    ids = {(await create_pending_order(sessionmaker, CHECKOUT)).id for _ in range(10)}
    await backdate(sessionmaker, 3600)

    first, second = await asyncio.gather(*(claim_stranded_orders(sessionmaker, 30, 10) for _ in range(2)))
    claimed_first, claimed_second = {i for i, _ in first}, {i for i, _ in second}
    assert claimed_first.isdisjoint(claimed_second)
    assert claimed_first | claimed_second == ids
    assert await claim_stranded_orders(sessionmaker, 30, 10) == []  # leased for another 30 s


async def test_two_sweepers_and_the_request_settle_one_order_once(sessionmaker):
    pending = await create_pending_order(sessionmaker, CHECKOUT)
    async with payments() as client_a, payments() as client_b:
        request_settle = settle_order(
            sessionmaker,
            pending.id,
            pending.total,
            ChargeOutcome("succeeded", charge_id=str(charge_id_for(pending.id))),
        )
        await asyncio.gather(
            Sweeper(sessionmaker, client_a, stale_after_s=0, meter=meter()).sweep_once(),
            Sweeper(sessionmaker, client_b, stale_after_s=0, meter=meter()).sweep_once(),
            request_settle,
        )
    rows = await query(sessionmaker, "SELECT status, provider_ref FROM payments WHERE order_id = :id", id=pending.id)
    assert rows == [("succeeded", str(charge_id_for(pending.id)))]


async def test_open_circuit_leaves_stranded_orders_pending(sessionmaker):
    await create_pending_order(sessionmaker, CHECKOUT)
    async with payments(breaker=BreakerConfig(min_calls=1, failure_ratio=1.0)) as client:
        client.breaker.record(client.breaker.allow(), failed=True)  # opened, as during game day 1
        assert await Sweeper(sessionmaker, client, stale_after_s=0, meter=meter()).sweep_once() == 0
    assert await query(sessionmaker, "SELECT status FROM orders") == [("pending",)]


async def test_checkout_whose_settle_failed_is_recovered_with_the_same_charge(
    seeded_db, shop_client, sessionmaker, monkeypatch, caplog
):
    """The ops-slot failure: payments charged, the settle transaction failed, the order stayed pending."""

    async def broken_settle(*args, **kwargs):
        raise TimeoutError("QueuePool limit reached")  # what the pool raised under load

    monkeypatch.setattr(orders.main, "settle_order", broken_settle)
    with caplog.at_level(logging.ERROR, logger="orders"):
        async with shop_client(seeded_db) as client:
            assert (await client.post("/checkout", json=GATEWAY_CHECKOUT)).status_code == 502
    logged = next(r for r in caplog.records if r.message == "settle failed, order left pending")
    assert await query(sessionmaker, "SELECT status FROM orders") == [("pending",)]

    async with payments() as client:
        assert await Sweeper(sessionmaker, client, stale_after_s=0, meter=meter()).sweep_once() == 1
    rows = await query(
        sessionmaker, "SELECT o.status, p.provider_ref FROM orders o JOIN payments p ON p.order_id = o.id"
    )
    assert rows == [("paid", logged.charge_id)]  # the charge the request made, not a second one


async def test_checkout_without_an_answer_from_payments_is_settled_by_the_sweeper(seeded_db, shop_client, sessionmaker):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with shop_client(seeded_db, payments_transport=httpx.MockTransport(timeout)) as client:
        response = await client.post("/checkout", json=GATEWAY_CHECKOUT)
    assert (response.status_code, response.json()["status"]) == (504, "pending")

    async with payments() as client:
        await Sweeper(sessionmaker, client, stale_after_s=0, meter=meter()).sweep_once()
    assert await query(sessionmaker, "SELECT status FROM orders") == [("paid",)]
