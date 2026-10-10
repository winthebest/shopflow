"""Settling is idempotent: concurrent settles of one order write one payment row and agree on the status."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from orders.db import create_engine
from orders.payments_client import ChargeOutcome
from orders.repository import create_pending_order, settle_order
from shopflow_common.schemas import CheckoutRequest

pytestmark = pytest.mark.integration

CHECKOUT = CheckoutRequest(customer_id=1, items=[{"product_id": 1, "quantity": 2}])


@pytest.fixture
async def sessionmaker(seeded_db):
    engine = create_engine(seeded_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def payments_of(sessionmaker, order_id: int) -> list[tuple]:
    async with sessionmaker() as session:
        rows = await session.execute(
            text(
                "SELECT o.status, p.status, p.provider_ref FROM orders o JOIN payments p ON p.order_id = o.id "
                "WHERE o.id = :id"
            ),
            {"id": order_id},
        )
        return [tuple(row) for row in rows]


async def test_concurrent_settles_write_one_payment_and_agree_on_the_status(sessionmaker):
    """The request path and stranded-order recovery may settle the same order at once: exactly one wins."""
    pending = await create_pending_order(sessionmaker, CHECKOUT)
    outcomes = [
        ChargeOutcome("succeeded", charge_id="c-1"),
        ChargeOutcome("declined"),
        ChargeOutcome("error", error="timeout"),
    ] * 2
    results = await asyncio.gather(*(settle_order(sessionmaker, pending.id, pending.total, o) for o in outcomes))

    assert sum(r.settled_now for r in results) == 1
    winner = next(r for r in results if r.settled_now)
    assert {(r.status, r.updated_at) for r in results} == {(winner.status, winner.updated_at)}
    rows = await payments_of(sessionmaker, pending.id)
    assert len(rows) == 1
    assert rows[0][0] == winner.status


async def test_settling_a_settled_order_writes_nothing(sessionmaker):
    pending = await create_pending_order(sessionmaker, CHECKOUT)
    first = await settle_order(sessionmaker, pending.id, pending.total, ChargeOutcome("succeeded", charge_id="c-1"))
    again = await settle_order(sessionmaker, pending.id, pending.total, ChargeOutcome("declined"))

    assert first.settled_now and not again.settled_now
    assert (again.status, again.updated_at) == ("paid", first.updated_at)
    assert await payments_of(sessionmaker, pending.id) == [("paid", "succeeded", "c-1")]


async def test_pending_order_returns_what_the_response_needs(sessionmaker):
    pending = await create_pending_order(sessionmaker, CHECKOUT)
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text("SELECT customer_id, total, created_at FROM orders WHERE id = :id"), {"id": pending.id}
            )
        ).one()
    assert (pending.customer_id, pending.total, pending.created_at) == tuple(row)
    assert [(i.product_id, i.quantity, str(i.unit_price)) for i in pending.items] == [(1, 2, "24.90")]
