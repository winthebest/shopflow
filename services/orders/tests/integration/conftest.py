"""Real Postgres for integration tests: one container per session, schema built by the Alembic migrations."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from gateway.main import Settings as GatewaySettings
from gateway.main import create_app as create_gateway
from orders.db import create_engine
from orders.main import create_app as create_orders
from orders.migrate import upgrade
from orders.seed import seed
from orders.settings import Settings as OrdersSettings
from payments.main import Settings as PaymentsSettings
from payments.main import create_app as create_payments

# Same image as docker-compose.yml.
POSTGRES_IMAGE = "postgres:17.11@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer(POSTGRES_IMAGE, username="shop_app", password="test", dbname="shop", driver=None) as pg:
        yield pg


@pytest.fixture(scope="session")
def database_url(postgres: PostgresContainer) -> str:
    url = postgres.get_connection_url()
    upgrade(url)
    return url


@pytest.fixture
async def seeded_db(database_url: str) -> str:
    """Empty tables (ids restart at 1), then the demo seed."""
    engine = create_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE payments, order_items, orders, products, customers RESTART IDENTITY CASCADE"))
    await engine.dispose()
    await seed(database_url)
    return database_url


@asynccontextmanager
async def _shop_client(
    database_url: str, *, failure_rate: float = 0.0, payments_transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """gateway -> orders -> payments wired in-process (ASGI transports), orders on the real database."""
    payments_app = create_payments(PaymentsSettings(payment_latency_ms=0, payment_failure_rate=failure_rate))
    orders_app = create_orders(
        OrdersSettings(database_url=database_url, payments_url="http://payments"),
        payments_transport=payments_transport or httpx.ASGITransport(app=payments_app),
    )
    gateway_app = create_gateway(
        GatewaySettings(orders_url="http://orders"), orders_transport=httpx.ASGITransport(app=orders_app)
    )
    async with (
        orders_app.router.lifespan_context(orders_app),
        gateway_app.router.lifespan_context(gateway_app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway") as client,
    ):
        yield client


@pytest.fixture
def shop_client():
    """`async with shop_client(url, failure_rate=..., payments_transport=...) as client:`"""
    return _shop_client
