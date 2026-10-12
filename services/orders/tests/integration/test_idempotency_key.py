"""`Idempotency-Key` on POST /checkout, end to end (gateway -> orders -> payments, real Postgres): a retry with the
same key gets the order the first attempt created, never a second order or a second charge."""

import asyncio
import uuid

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from orders.db import create_engine
from orders.payments_client import PaymentsClient
from orders.repository import create_pending_order
from orders.sweeper import Sweeper
from payments.main import Settings as PaymentsSettings
from payments.main import create_app as create_payments
from shopflow_common.schemas import CheckoutRequest, request_fingerprint

pytestmark = pytest.mark.integration

CHECKOUT = {"customer_id": 1, "items": [{"product_id": 1, "quantity": 2}, {"product_id": 4, "quantity": 1}]}
KEY = {"Idempotency-Key": str(uuid.UUID(int=1))}  # built, not a literal: a UUID literal looks like a secret to gitleaks


@pytest.fixture
async def sessionmaker(seeded_db):
    engine = create_engine(seeded_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def rows(sessionmaker, sql: str) -> list[tuple]:
    async with sessionmaker() as session:
        return [tuple(row) for row in await session.execute(text(sql))]


async def test_retry_with_the_same_key_gets_the_same_order_and_one_charge(seeded_db, shop_client, sessionmaker):
    async with shop_client(seeded_db) as client:
        first = await client.post("/checkout", json=CHECKOUT, headers=KEY)
        retry = await client.post("/checkout", json=CHECKOUT, headers=KEY)

    assert (first.status_code, retry.status_code) == (201, 201)
    assert retry.json() == first.json()  # the same answer, read back from the stored order
    assert await rows(sessionmaker, "SELECT count(*) FROM orders") == [(1,)]
    assert await rows(sessionmaker, "SELECT count(*) FROM payments") == [(1,)]
    stored = await rows(sessionmaker, "SELECT idempotency_key, request_hash FROM orders")
    assert stored == [(KEY["Idempotency-Key"], request_fingerprint(CheckoutRequest.model_validate(CHECKOUT)))]


async def test_same_key_with_a_different_request_is_refused(seeded_db, shop_client, sessionmaker):
    other = {**CHECKOUT, "items": [{"product_id": 1, "quantity": 3}]}
    async with shop_client(seeded_db) as client:
        assert (await client.post("/checkout", json=CHECKOUT, headers=KEY)).status_code == 201
        refused = await client.post("/checkout", json=other, headers=KEY)

    assert refused.status_code == 422
    assert refused.json() == {"detail": "Idempotency-Key was already used with a different request"}
    assert await rows(sessionmaker, "SELECT count(*) FROM orders") == [(1,)]


async def test_key_of_a_checkout_still_in_progress_gets_409_with_retry_after(seeded_db, shop_client, sessionmaker):
    request = CheckoutRequest.model_validate(CHECKOUT)
    pending = await create_pending_order(
        sessionmaker, request, KEY["Idempotency-Key"], request_fingerprint(request)
    )  # the first attempt has created its order and is waiting for payments
    async with shop_client(seeded_db) as client:
        response = await client.post("/checkout", json=CHECKOUT, headers=KEY)

    assert (response.status_code, response.headers["retry-after"]) == (409, "1")
    assert response.json()["order_id"] == pending.id
    assert response.json()["status"] == "pending"
    assert await rows(sessionmaker, "SELECT count(*) FROM payments") == [(0,)]  # the retry charged nothing


async def test_concurrent_checkouts_with_one_key_create_one_order(seeded_db, shop_client, sessionmaker):
    async with shop_client(seeded_db) as client:
        responses = await asyncio.gather(*(client.post("/checkout", json=CHECKOUT, headers=KEY) for _ in range(4)))

    codes = sorted(r.status_code for r in responses)
    assert set(codes) <= {201, 409} and 201 in codes
    assert len({r.json()["id"] for r in responses if r.status_code == 201}) == 1
    assert await rows(sessionmaker, "SELECT count(*) FROM orders") == [(1,)]
    assert await rows(sessionmaker, "SELECT count(*) FROM payments") == [(1,)]


async def test_timed_out_checkout_retried_with_its_key_gets_the_order_once_settled(
    seeded_db, shop_client, sessionmaker
):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with shop_client(seeded_db, payments_transport=httpx.MockTransport(timeout)) as client:
        first = await client.post("/checkout", json=CHECKOUT, headers=KEY)
        early_retry = await client.post("/checkout", json=CHECKOUT, headers=KEY)
    assert (first.status_code, first.json()["status"]) == (504, "pending")
    assert early_retry.status_code == 409  # not settled yet: wait, do not create another order

    payments_app = create_payments(PaymentsSettings(payment_latency_ms=0, payment_failure_rate=0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=payments_app), base_url="http://payments") as http:
        meter = MeterProvider().get_meter("test")
        sweeper = Sweeper(sessionmaker, PaymentsClient(http, meter=meter), stale_after_s=0, meter=meter)
        assert await sweeper.sweep_once() == 1

    async with shop_client(seeded_db) as client:
        late_retry = await client.post("/checkout", json=CHECKOUT, headers=KEY)
    assert (late_retry.status_code, late_retry.json()["status"]) == (201, "paid")
    assert late_retry.json()["id"] == first.json()["order_id"]
    assert await rows(sessionmaker, "SELECT count(*) FROM orders") == [(1,)]


async def test_invalid_key_is_rejected_before_anything_is_written(seeded_db, shop_client, sessionmaker):
    async with shop_client(seeded_db) as client:
        response = await client.post("/checkout", json=CHECKOUT, headers={"Idempotency-Key": "x" * 65})
    assert response.status_code == 422
    assert await rows(sessionmaker, "SELECT count(*) FROM orders") == [(0,)]
