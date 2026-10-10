"""Settings validation and the probe endpoints (fake consumer, unreachable database: no Docker needed)."""

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from fulfillment_worker.main import create_app
from fulfillment_worker.settings import Settings
from shopflow_common.db import async_dsn

UNREACHABLE_DB = "postgresql://fulfillment_worker:x@127.0.0.1:1/shop"


def test_database_url_from_parts_is_percent_encoded_and_maps_sslmode():
    settings = Settings(
        kafka_bootstrap_servers="kafka:9092",
        kafka_security_protocol="PLAINTEXT",
        database_host="shop-db-rw.shop.svc",
        database_user="fulfillment_worker",
        database_password="p@ss/w:rd%",
        database_sslmode="require",
    )
    assert (
        settings.dsn()
        == "postgresql://fulfillment_worker:p%40ss%2Fw%3Ard%25@shop-db-rw.shop.svc:5432/shop?sslmode=require"
    )
    assert async_dsn(settings.dsn()).startswith("postgresql+asyncpg://fulfillment_worker:p%40ss%2Fw%3Ard%25@")
    assert async_dsn(settings.dsn()).endswith("/shop?ssl=require")


def test_database_settings_are_required():
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(kafka_bootstrap_servers="kafka:9092", kafka_security_protocol="PLAINTEXT")


def test_sasl_ssl_needs_credentials_and_ca():
    with pytest.raises(ValidationError, match="KAFKA_USERNAME"):
        Settings(kafka_bootstrap_servers="kafka:9093", database_url=UNREACHABLE_DB)
    Settings(
        kafka_bootstrap_servers="kafka:9093",
        database_url=UNREACHABLE_DB,
        kafka_username="fulfillment-worker",
        kafka_password="x",
        kafka_ca_file="/etc/kafka/ca.crt",
    )


class IdleConsumer:
    """Starts, never returns records; `fail` makes getmany raise (a dead loop)."""

    def __init__(self, fail: bool = False) -> None:
        self.fail, self.started, self.stopped = fail, False, False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def getmany(self, *partitions, timeout_ms: int, max_records: int | None) -> dict:
        if self.fail:
            raise RuntimeError("broker gone")
        await asyncio.sleep(0.01)
        return {}

    async def commit(self) -> None:
        pass


def app_with(consumer: IdleConsumer):
    settings = Settings(
        kafka_bootstrap_servers="unused:9092", kafka_security_protocol="PLAINTEXT", database_url=UNREACHABLE_DB
    )
    return create_app(settings, consumer=consumer)


async def probe(app, path: str) -> int:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        return (await client.get(path)).status_code


async def test_healthy_loop_is_live_but_not_ready_without_database():
    consumer = IdleConsumer()
    app = app_with(consumer)
    async with app.router.lifespan_context(app):
        assert consumer.started
        assert await probe(app, "/healthz") == 200
        assert await probe(app, "/readyz") == 503  # database unreachable
    assert consumer.stopped  # leaves the group on shutdown


async def test_dead_loop_fails_liveness():
    app = app_with(IdleConsumer(fail=True))
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)  # let the loop crash
        assert await probe(app, "/healthz") == 503
        assert await probe(app, "/readyz") == 503


async def test_stalled_loop_fails_liveness(monkeypatch):
    app = app_with(IdleConsumer())
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(app.state.worker, "seconds_since_poll", lambda: 500.0)
        assert await probe(app, "/healthz") == 503


@pytest.mark.parametrize(
    ("latency_ms", "batch", "ok"),
    [(20, 500, True), (120, 500, True), (300, 200, True), (300, 500, False), (10_000, 7, False)],
)
def test_batch_latency_must_stay_below_the_stall_bound(latency_ms, batch, ok):
    def build() -> Settings:
        return Settings(
            kafka_bootstrap_servers="kafka:9092",
            kafka_security_protocol="PLAINTEXT",
            database_url=UNREACHABLE_DB,
            shipment_latency_ms=latency_ms,
            batch_max_records=batch,
        )

    if ok:
        build()
    else:
        with pytest.raises(ValidationError, match=r"set BATCH_MAX_RECORDS <= \d+"):
            build()


def test_stall_bound_leaves_room_for_the_slowest_allowed_batch():
    from fulfillment_worker.main import STALLED_AFTER_S
    from fulfillment_worker.settings import MAX_BATCH_LATENCY_MS

    assert MAX_BATCH_LATENCY_MS / 1000 <= STALLED_AFTER_S / 2
