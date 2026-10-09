"""End-to-end checkout through gateway -> orders -> payments on real Postgres."""

import asyncio
import logging
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

import orders.main
from orders.db import create_engine
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


async def test_payments_timeout_fails_order_and_returns_504(seeded_db, shop_client):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with shop_client(seeded_db, payments_transport=httpx.MockTransport(timeout)) as client:
        response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 504
    body = response.json()
    assert body["status"] == "failed"
    rows = await fetch(
        seeded_db,
        "SELECT o.status, p.status FROM orders o JOIN payments p ON p.order_id = o.id WHERE o.id = :id",
        id=body["order_id"],
    )
    assert rows == [("failed", "error")]


def _non_json_201(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, text="<html>ok</html>")


def _unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize("handler", [_non_json_201, _unavailable, _refused], ids=["non-json-201", "503", "refused"])
async def test_payments_failure_still_settles_order_as_failed(seeded_db, shop_client, handler):
    async with shop_client(seeded_db, payments_transport=httpx.MockTransport(handler)) as client:
        response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 502
    assert response.json()["status"] == "failed"
    rows = await fetch(seeded_db, "SELECT o.status, p.status FROM orders o JOIN payments p ON p.order_id = o.id")
    assert rows == [("failed", "error")]


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
