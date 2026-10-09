"""End-to-end checkout through gateway -> orders -> payments on real Postgres."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from orders.db import create_engine

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
