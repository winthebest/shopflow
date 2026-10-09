"""Alembic owns the schema: models must match the migrations, and migrations must apply and roll back cleanly."""

import asyncio

import httpx
import pytest
from alembic import command
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from orders.db import create_engine
from orders.main import create_app
from orders.migrate import alembic_config, upgrade
from orders.settings import Settings

pytestmark = pytest.mark.integration


def test_models_match_migrations(database_url):
    command.check(alembic_config(database_url))  # raises if autogenerate would produce any operation


def test_upgrade_is_idempotent(database_url):
    upgrade(database_url)
    command.check(alembic_config(database_url))


def test_downgrade_and_upgrade_roundtrip(postgres: PostgresContainer):
    """On a separate database so the shared one stays at head."""
    admin_url = postgres.get_connection_url()

    async def create_database() -> None:
        engine = create_engine(admin_url).execution_options(isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text("DROP DATABASE IF EXISTS roundtrip"))
            await conn.execute(text("CREATE DATABASE roundtrip"))
        await engine.dispose()

    asyncio.run(create_database())
    url = admin_url.rsplit("/", 1)[0] + "/roundtrip"
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)


async def test_updated_at_trigger(seeded_db):
    engine = create_engine(seeded_db)
    async with engine.begin() as conn:
        before = (await conn.execute(text("SELECT updated_at FROM products WHERE id = 1"))).scalar_one()
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE products SET price = price + 1 WHERE id = 1"))
    async with engine.begin() as conn:
        after = (await conn.execute(text("SELECT updated_at FROM products WHERE id = 1"))).scalar_one()
    await engine.dispose()
    assert after > before


async def test_readyz_checks_database(database_url):
    app = create_app(Settings(database_url=database_url))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://orders") as client,
    ):
        assert (await client.get("/readyz")).status_code == 200

    broken = create_app(Settings(database_url="postgresql://nobody:x@127.0.0.1:1/none"))
    async with (
        broken.router.lifespan_context(broken),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=broken), base_url="http://orders") as client,
    ):
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/healthz")).status_code == 200
