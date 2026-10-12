import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

import gateway.main as gateway_main
from gateway.main import Settings, create_app

CHECKOUT = {"customer_id": 1, "items": [{"product_id": 2, "quantity": 1}]}


@asynccontextmanager
async def gateway_with(handler: Callable) -> AsyncIterator[httpx.AsyncClient]:
    """Gateway app whose `orders` dependency is answered by `handler`."""
    app = create_app(Settings(orders_url="http://orders"), orders_transport=httpx.MockTransport(handler))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client,
    ):
        yield client


async def test_checkout_forwards_to_orders_and_passes_response_through():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 7, "status": "paid"})

    async with gateway_with(handler) as client:
        response = await client.post("/checkout", json=CHECKOUT)

    assert response.status_code == 201
    assert response.json() == {"id": 7, "status": "paid"}
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/orders"


async def test_invalid_input_is_rejected_without_calling_orders():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("orders must not be called")

    async with gateway_with(handler) as client:
        response = await client.post("/checkout", json={"customer_id": 1, "items": []})
        bad_id = await client.get("/orders/0")

    assert response.status_code == 422
    assert bad_id.status_code == 422


@pytest.mark.parametrize("status", [404, 422])
async def test_client_errors_pass_through(status):
    async with gateway_with(lambda request: httpx.Response(status, json={"detail": "x"})) as client:
        response = await client.get("/orders/5")
    assert response.status_code == status
    assert response.json() == {"detail": "x"}


async def test_orders_timeout_becomes_504():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with gateway_with(handler) as client:
        response = await client.get("/products")
    assert response.status_code == 504


async def test_total_deadline_is_enforced(monkeypatch):
    monkeypatch.setattr(gateway_main, "ORDERS_TIMEOUT_S", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=[])

    async with gateway_with(handler) as client:
        response = await client.get("/products")
    assert response.status_code == 504


async def test_orders_unreachable_becomes_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with gateway_with(handler) as client:
        response = await client.get("/products")
    assert response.status_code == 502


async def test_orders_500_becomes_502():
    async with gateway_with(lambda request: httpx.Response(500, text="boom")) as client:
        response = await client.get("/products")
    assert response.status_code == 502
    assert response.json()["upstream_status"] == 500


async def test_open_payments_circuit_503_passes_through_with_retry_after():
    body = {"detail": "payments unavailable (circuit open)"}
    async with gateway_with(lambda request: httpx.Response(503, json=body, headers={"Retry-After": "4"})) as client:
        response = await client.post("/checkout", json=CHECKOUT)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "4"
    assert response.json() == body


@pytest.mark.parametrize("status", [502, 504])
async def test_orders_dependency_errors_keep_status_and_body(status):
    body = {"detail": "payments timed out", "order_id": 9, "status": "failed"}
    async with gateway_with(lambda request: httpx.Response(status, json=body)) as client:
        response = await client.post("/checkout", json=CHECKOUT)
    assert response.status_code == status
    assert response.json() == body


async def test_stays_ready_when_orders_is_down():
    """Readiness has no dependencies, so the gateway keeps answering 502 itself while orders is unreachable."""

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with gateway_with(down) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/products")).status_code == 502


async def test_idempotency_key_is_forwarded_and_409_passes_through_with_retry_after():
    seen: list[httpx.Request] = []
    body = {"detail": "a checkout with this Idempotency-Key is in progress", "order_id": 7, "status": "pending"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(409, json=body, headers={"Retry-After": "1"})

    async with gateway_with(handler) as client:
        response = await client.post("/checkout", json=CHECKOUT, headers={"Idempotency-Key": "k-1"})
    assert seen[0].headers["idempotency-key"] == "k-1"
    assert (response.status_code, response.headers["retry-after"], response.json()) == (409, "1", body)


async def test_checkout_without_a_key_sends_none():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 1})

    async with gateway_with(handler) as client:
        await client.post("/checkout", json=CHECKOUT)
    assert "idempotency-key" not in seen[0].headers


@pytest.mark.parametrize("key", ["x" * 65, "has space"], ids=["too-long", "space"])
async def test_invalid_idempotency_key_is_rejected_at_the_edge(key):
    calls: list[httpx.Request] = []
    async with gateway_with(lambda request: calls.append(request) or httpx.Response(201, json={})) as client:
        response = await client.post("/checkout", json=CHECKOUT, headers={"Idempotency-Key": key})
    assert response.status_code == 422
    assert calls == []
