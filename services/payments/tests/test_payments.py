import time

import httpx
import pytest
from pydantic import ValidationError

from payments.main import Settings, create_app


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
