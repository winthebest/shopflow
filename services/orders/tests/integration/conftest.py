"""Real Postgres for integration tests: one container per session, schema built by the Alembic migrations.

Set up like CNPG and compose: `wal_level=logical`, and the app role owns its database without being a superuser.
"""

from collections.abc import AsyncIterator, Callable, Iterator
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


APP_ROLE = "shop_app"
APP_PASSWORD = "test-only"
CDC_ROLE = "debezium"
CDC_PASSWORD = "test-only"
TRINO_ROLE = "trino_pg"
TRINO_PASSWORD = "test-only"
WORKER_ROLE = "fulfillment_worker"
WORKER_PASSWORD = "test-only"


def psql(pg: PostgresContainer, sql: str) -> None:
    result = pg.exec(["psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql])
    assert result.exit_code == 0, result.output


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    container = PostgresContainer(
        POSTGRES_IMAGE, username="postgres", password="test-only", dbname="postgres", driver=None
    ).with_command("postgres -c wal_level=logical")
    with container as pg:
        psql(pg, f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
        psql(pg, f"CREATE ROLE {CDC_ROLE} LOGIN REPLICATION PASSWORD '{CDC_PASSWORD}'")
        psql(pg, f"CREATE ROLE {TRINO_ROLE} LOGIN PASSWORD '{TRINO_PASSWORD}'")
        psql(pg, f"CREATE ROLE {WORKER_ROLE} LOGIN PASSWORD '{WORKER_PASSWORD}'")
        yield pg


def connection_url(pg: PostgresContainer, user: str, password: str, dbname: str) -> str:
    return f"postgresql://{user}:{password}@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{dbname}"


@pytest.fixture(scope="session")
def app_database(postgres: PostgresContainer) -> Callable[[str], str]:
    """`app_database(name)` creates a database owned by the app role and returns its app-role URL."""

    def create(name: str) -> str:
        psql(postgres, f"CREATE DATABASE {name} OWNER {APP_ROLE}")
        return connection_url(postgres, APP_ROLE, APP_PASSWORD, name)

    return create


@pytest.fixture(scope="session")
def database_url(app_database: Callable[[str], str]) -> str:
    url = app_database("shop")
    upgrade(url)
    return url


@pytest.fixture(scope="session")
def superuser_sql(postgres: PostgresContainer) -> Callable[[str], None]:
    """Run one SQL statement as the `postgres` superuser (role setup that the app role may not do)."""
    return lambda sql: psql(postgres, sql)


@pytest.fixture(scope="session")
def cdc_url(postgres: PostgresContainer, database_url: str) -> str:
    """The migrated `shop` database, connected as the CDC role."""
    return connection_url(postgres, CDC_ROLE, CDC_PASSWORD, "shop")


@pytest.fixture(scope="session")
def trino_url(postgres: PostgresContainer, database_url: str) -> str:
    """The migrated `shop` database, connected as the Trino `pg` catalog role."""
    return connection_url(postgres, TRINO_ROLE, TRINO_PASSWORD, "shop")


@pytest.fixture(scope="session")
def worker_url(postgres: PostgresContainer, database_url: str) -> str:
    """The migrated `shop` database, connected as the fulfillment-worker role."""
    return connection_url(postgres, WORKER_ROLE, WORKER_PASSWORD, "shop")


@pytest.fixture
async def seeded_db(database_url: str) -> str:
    """Empty tables (ids restart at 1), then the demo seed."""
    engine = create_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE shipments, payments, order_items, orders, products, customers RESTART IDENTITY CASCADE")
        )
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
        GatewaySettings(orders_url="http://orders"),
        # Unhandled errors in orders become a 500 response, as over a real network.
        orders_transport=httpx.ASGITransport(app=orders_app, raise_app_exceptions=False),
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
