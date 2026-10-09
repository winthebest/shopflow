"""Alembic owns the schema: models must match the migrations, and migrations must apply and roll back cleanly."""

import asyncio

import httpx
import pytest
from alembic import command
from sqlalchemy import text

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


def test_downgrade_and_upgrade_roundtrip(app_database):
    """On a separate database so the shared one stays at head; runs as the non-superuser app role."""
    config = alembic_config(app_database("roundtrip"))
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)


def test_wal_level_is_logical(database_url):
    async def wal_level() -> str:
        engine = create_engine(database_url)
        async with engine.connect() as conn:
            value = (await conn.execute(text("SHOW wal_level"))).scalar_one()
        await engine.dispose()
        return value

    assert asyncio.run(wal_level()) == "logical"


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
