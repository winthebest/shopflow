"""End-to-end checkout through gateway -> orders -> payments on real Postgres."""

import asyncio
import json
import logging
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import orders.main
from orders.db import create_engine
from orders.payments_client import ChargeOutcome
from orders.repository import settle_order
from orders.seed import seed

pytestmark = pytest.mark.integration

# Seeded prices: product 1 = 24.90, product 4 = 9.00.
CHECKOUT = {"customer_id": 1, "items": [{"product_id": 1, "quantity": 2}, {"product_id": 4, "quantity": 1}]}


async def fetch(database_url: str, sql: str, **params) -> list[tuple]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as conn:
            return [tuple(row) for row in await conn.execute(text(sql), params)]
    finally:
        await engine.dispose()


async def test_paid_checkout(seeded_db, shop_client):
    async with shop_client(seeded_db, failure_rate=0.0) as client:
        created = await client.post("/checkout", json=CHECKOUT)
        assert created.status_code == 201
        order = created.json()
        fetched = await client.get(f"/orders/{order['id']}")

    assert order["status"] == "paid"
    assert Decimal(order["total"]) == Decimal("58.80")
    assert [(i["product_id"], i["quantity"], i["unit_price"]) for i in order["items"]] == [
        (1, 2, "24.90"),
        (4, 1, "9.00"),
    ]
    assert fetched.status_code == 200
    assert fetched.json() == order

    rows = await fetch(
        seeded_db,
        "SELECT o.status, p.status, p.amount, p.provider_ref IS NOT NULL, o.updated_at > o.created_at "
        "FROM orders o JOIN payments p ON p.order_id = o.id WHERE o.id = :id",
        id=order["id"],
    )
    assert rows == [("paid", "succeeded", Decimal("58.80"), True, True)]


async def test_declined_payment_fails_order(seeded_db, shop_client):
    async with shop_client(seeded_db, failure_rate=1.0) as client:
        created = await client.post("/checkout", json=CHECKOUT)

    assert created.status_code == 201
    assert created.json()["status"] == "failed"
    rows = await fetch(seeded_db, "SELECT o.status, p.status FROM orders o JOIN payments p ON p.order_id = o.id")
    assert rows == [("failed", "declined")]


async def test_payments_timeout_leaves_order_pending_and_returns_504(seeded_db, shop_client):
    """A timeout may come after payments charged: the sweeper settles the order with the real answer later."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with shop_client(seeded_db, payments_transport=httpx.MockTransport(timeout)) as client:
        response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 504
    body = response.json()
    assert body["status"] == "pending"
    assert await fetch(seeded_db, "SELECT status FROM orders WHERE id = :id", id=body["order_id"]) == [("pending",)]
    assert await fetch(seeded_db, "SELECT count(*) FROM payments") == [(0,)]


def _non_json_201(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, text="<html>ok</html>")


def _unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize("handler", [_non_json_201, _unavailable, _refused], ids=["non-json-201", "503", "refused"])
async def test_payments_failure_leaves_order_pending_and_returns_502(seeded_db, shop_client, handler):
    async with shop_client(seeded_db, payments_transport=httpx.MockTransport(handler)) as client:
        response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 502
    assert response.json()["status"] == "pending"
    assert await fetch(seeded_db, "SELECT status FROM orders") == [("pending",)]
    assert await fetch(seeded_db, "SELECT count(*) FROM payments") == [(0,)]


async def test_retried_charge_is_idempotent_and_settles_the_order_once(seeded_db, shop_client):
    """payments drops the first answer (503); the retry gets the charge, and the order has exactly one payment."""
    from payments.main import Settings as PaymentsSettings
    from payments.main import charge_id_for
    from payments.main import create_app as create_payments

    real = httpx.ASGITransport(app=create_payments(PaymentsSettings(payment_latency_ms=0, payment_failure_rate=0)))
    attempts = []

    class FlakyOnce(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.path)
            return httpx.Response(503) if len(attempts) == 1 else await real.handle_async_request(request)

    async with shop_client(seeded_db, payments_transport=FlakyOnce()) as client:
        response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 201
    assert attempts == ["/charges", "/charges"]
    rows = await fetch(
        seeded_db,
        "SELECT o.status, p.status, p.provider_ref FROM orders o JOIN payments p ON p.order_id = o.id WHERE o.id = :id",
        id=response.json()["id"],
    )
    assert rows == [("paid", "succeeded", str(charge_id_for(response.json()["id"])))]


async def test_open_circuit_answers_503_without_creating_an_order(seeded_db, shop_client):
    async with shop_client(seeded_db, payments_transport=httpx.MockTransport(_unavailable)) as client:
        for _ in range(7):  # 3 failed attempts per checkout; the 20th failed attempt opens the circuit (min_calls)
            assert (await client.post("/checkout", json=CHECKOUT)).status_code == 502
        orders_before = await fetch(seeded_db, "SELECT count(*) FROM orders")
        refused = await client.post("/checkout", json=CHECKOUT)

    assert refused.status_code == 503
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.json() == {"detail": "payments unavailable (circuit open)"}
    assert await fetch(seeded_db, "SELECT count(*) FROM orders") == orders_before  # nothing to clean up


async def test_settle_failure_is_logged_for_reconciliation(seeded_db, shop_client, monkeypatch, caplog):
    async def broken_settle(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(orders.main, "settle_order", broken_settle)
    with caplog.at_level(logging.ERROR, logger="orders"):
        async with shop_client(seeded_db) as client:
            response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 502  # orders 500 -> gateway 502
    record = next(r for r in caplog.records if r.message == "settle failed, order left pending")
    assert record.order_id == 1
    assert record.payment_status == "succeeded"
    assert record.charge_id
    assert await fetch(seeded_db, "SELECT status FROM orders") == [("pending",)]


async def test_checkout_answer_does_not_read_the_order_back(seeded_db, shop_client, monkeypatch):
    """A read after settling can fail (e.g. no free connection) and lose the acknowledgement of a paid order."""

    async def no_reads(*args, **kwargs):
        raise AssertionError("checkout must not read the order back after settling")

    monkeypatch.setattr(orders.main, "get_order", no_reads)
    async with shop_client(seeded_db) as client:
        response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 201
    assert response.json()["status"] == "paid"


async def test_checkout_that_loses_the_settle_race_answers_the_winners_status(seeded_db, shop_client, caplog):
    """Something else settled the order while the charge was in flight: the request writes nothing and reports it."""
    engine = create_engine(seeded_db)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    class SettledMeanwhile(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            order_id = json.loads(request.read())["order_id"]
            await settle_order(sessionmaker, order_id, Decimal("58.80"), ChargeOutcome("declined"))
            return httpx.Response(201, json={"charge_id": "c-late", "status": "succeeded"})

    try:
        with caplog.at_level(logging.INFO, logger="orders"):
            async with shop_client(seeded_db, payments_transport=SettledMeanwhile()) as client:
                response = await client.post("/checkout", json=CHECKOUT)
    finally:
        await engine.dispose()

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert any(r.message == "order already settled" for r in caplog.records)
    assert await fetch(seeded_db, "SELECT status, provider_ref FROM payments") == [("declined", None)]


async def test_total_beyond_numeric_12_2_is_rejected(seeded_db, shop_client):
    engine = create_engine(seeded_db)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE products SET price = 9999999999.99 WHERE id = 1"))
    await engine.dispose()

    async with shop_client(seeded_db) as client:
        response = await client.post("/checkout", json={"customer_id": 1, "items": [{"product_id": 1, "quantity": 2}]})

    assert response.status_code == 422
    assert await fetch(seeded_db, "SELECT count(*) FROM orders") == [(0,)]


async def test_reseeding_inserts_nothing_and_keeps_identity_sequences(seeded_db):
    await seed(seeded_db)
    rows = await fetch(
        seeded_db,
        "SELECT (SELECT count(*) FROM products), "
        "(SELECT last_value FROM products_id_seq), (SELECT last_value FROM customers_id_seq)",
    )
    assert rows == [(20, 20, 100)]


async def test_unknown_product_writes_nothing(seeded_db, shop_client):
    async with shop_client(seeded_db) as client:
        response = await client.post(
            "/checkout", json={"customer_id": 1, "items": [{"product_id": 999, "quantity": 1}]}
        )
        unknown_customer = await client.post(
            "/checkout", json={"customer_id": 999, "items": [{"product_id": 1, "quantity": 1}]}
        )

    assert response.status_code == 422
    assert "999" in response.json()["detail"]
    assert unknown_customer.status_code == 422
    assert await fetch(seeded_db, "SELECT count(*) FROM orders") == [(0,)]
    assert await fetch(seeded_db, "SELECT count(*) FROM order_items") == [(0,)]


async def test_concurrent_checkouts_are_all_settled(seeded_db, shop_client):
    async with shop_client(seeded_db) as client:
        responses = await asyncio.gather(
            *(
                client.post("/checkout", json={"customer_id": n, "items": [{"product_id": n, "quantity": 1}]})
                for n in range(1, 21)
            )
        )

    assert [r.status_code for r in responses] == [201] * 20
    assert await fetch(seeded_db, "SELECT status, count(*) FROM orders GROUP BY status") == [("paid", 20)]
    assert await fetch(seeded_db, "SELECT count(*) FROM payments") == [(20,)]


async def test_products_and_missing_order(seeded_db, shop_client):
    async with shop_client(seeded_db) as client:
        products = await client.get("/products")
        missing = await client.get("/orders/123456")

    assert products.status_code == 200
    assert len(products.json()) == 20
    assert products.json()[0] == {"id": 1, "sku": "SKU-0001", "name": "Espresso beans 1kg", "price": "24.90"}
    assert missing.status_code == 404
