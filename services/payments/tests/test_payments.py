import time

import httpx
import pytest
from pydantic import ValidationError

from payments.main import Settings, create_app, declined


def client_for(**settings) -> httpx.AsyncClient:
    app = create_app(Settings(**settings))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://payments")


async def test_charge_succeeds_when_failure_rate_is_zero():
    async with client_for(payment_latency_ms=0, payment_failure_rate=0) as client:
        response = await client.post("/charges", json={"order_id": 1, "amount": "10.00"})
    assert response.status_code == 201
    assert response.json()["status"] == "succeeded"
    assert response.json()["charge_id"]


async def test_charge_declined_when_failure_rate_is_one():
    async with client_for(payment_latency_ms=0, payment_failure_rate=1) as client:
        response = await client.post("/charges", json={"order_id": 1, "amount": "10.00"})
    assert response.status_code == 402
    assert response.json() == {"charge_id": None, "status": "declined"}


async def test_charge_is_idempotent_per_order():
    """A retry, or the same order on another replica, gets the same answer and the same charge_id."""
    async with (
        client_for(payment_latency_ms=0, payment_failure_rate=0.5) as client,
        client_for(payment_latency_ms=0, payment_failure_rate=0.5) as other_replica,
    ):
        for order_id in range(1, 21):
            body = {"order_id": order_id, "amount": "10.00"}
            first, retry, elsewhere = [await c.post("/charges", json=body) for c in (client, client, other_replica)]
            assert first.status_code == retry.status_code == elsewhere.status_code
            assert first.json() == retry.json() == elsewhere.json()


async def test_different_orders_get_different_charge_ids():
    async with client_for(payment_latency_ms=0, payment_failure_rate=0) as client:
        answers = [await client.post("/charges", json={"order_id": i, "amount": "1.00"}) for i in (1, 2)]
    ids = {answer.json()["charge_id"] for answer in answers}
    assert len(ids) == 2


@pytest.mark.parametrize("rate", [0.0, 0.02, 0.5, 1.0])
def test_decline_share_matches_the_failure_rate(rate):
    share = sum(declined(order_id, rate) for order_id in range(1, 100_001)) / 100_000
    assert share == pytest.approx(rate, abs=0.003)


async def test_latency_is_applied():
    async with client_for(payment_latency_ms=150, payment_failure_rate=0) as client:
        start = time.perf_counter()
        response = await client.post("/charges", json={"order_id": 1, "amount": "1.00"})
    assert response.status_code == 201
    assert time.perf_counter() - start >= 0.15


@pytest.mark.parametrize(
    "body",
    [{"order_id": 1, "amount": "0"}, {"order_id": 0, "amount": "1"}, {"order_id": 1, "amount": "1.001"}, {}],
)
async def test_invalid_charge_rejected(body):
    async with client_for(payment_latency_ms=0) as client:
        response = await client.post("/charges", json=body)
    assert response.status_code == 422


async def test_health_endpoints():
    async with client_for() as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200


@pytest.mark.parametrize("env", [{"PAYMENT_FAILURE_RATE": "1.5"}, {"PAYMENT_LATENCY_MS": "-1"}])
def test_bad_config_fails_fast(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()
