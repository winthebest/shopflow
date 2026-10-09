"""Migration 0002: CDC source objects as specified in docs/contracts/services.md ("CDC source objects")."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from orders.db import create_engine
from orders.migrate import alembic_config, upgrade

pytestmark = pytest.mark.integration

PUBLISHED = {"customers", "products", "orders", "order_items", "payments", "heartbeat"}


async def query(url: str, sql: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            return [tuple(row) for row in await conn.execute(text(sql))]
    finally:
        await engine.dispose()


async def test_publication_lists_exactly_the_six_source_tables(database_url):
    tables = await query(
        database_url, "SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname = 'shop_cdc'"
    )
    all_tables = await query(database_url, "SELECT puballtables FROM pg_publication WHERE pubname = 'shop_cdc'")

    assert set(tables) == {("public", table) for table in PUBLISHED}
    assert all_tables == [(False,)]  # explicit list, never FOR ALL TABLES; meta is not published


async def test_heartbeat_has_exactly_one_row(database_url):
    assert await query(database_url, "SELECT id FROM heartbeat") == [(1,)]
    with pytest.raises(DBAPIError, match="ck_heartbeat_single_row"):
        await query(database_url, "INSERT INTO heartbeat (id) VALUES (2)")


async def test_debezium_role_is_replication_only(database_url):
    rows = await query(
        database_url,
        "SELECT rolreplication, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'debezium'",
    )
    assert rows == [(True, False, False, False)]


@pytest.mark.parametrize("table", sorted(PUBLISHED))
async def test_debezium_can_read_every_published_table(cdc_url, table):
    await query(cdc_url, f"SELECT * FROM {table} LIMIT 1")  # noqa: S608 - fixed table names


async def test_debezium_can_beat_the_heartbeat(cdc_url):
    assert await query(cdc_url, "UPDATE heartbeat SET beat_at = now() WHERE id = 1 RETURNING id") == [(1,)]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders (customer_id, status, total) VALUES (1, 'pending', 1)",
        "UPDATE orders SET status = 'paid'",
        "DELETE FROM payments",
        "SELECT * FROM meta.cdc_epochs",
        "CREATE TABLE public.sneaky (id int)",
    ],
    ids=["insert-orders", "update-orders", "delete-payments", "read-meta", "create-table"],
)
async def test_debezium_cannot_write_or_read_outside_its_grants(cdc_url, sql):
    with pytest.raises(DBAPIError, match="permission denied"):
        await query(cdc_url, sql)


def test_migration_fails_clearly_when_debezium_role_is_missing(app_database, superuser_sql):
    url = app_database("no_cdc_role")
    superuser_sql("ALTER ROLE debezium RENAME TO debezium_away")
    try:
        with pytest.raises(DBAPIError, match='CDC role "debezium" is missing: create it before migrating'):
            upgrade(url)
    finally:
        superuser_sql("ALTER ROLE debezium_away RENAME TO debezium")
    # One transaction for the whole upgrade: nothing from 0001 or 0002 is left half-applied.
    untouched = (
        "SELECT to_regclass('public.alembic_version') IS NULL, to_regclass('public.orders') IS NULL,"
        " to_regclass('public.heartbeat') IS NULL"
    )
    assert asyncio.run(query(url, untouched)) == [(True, True, True)]


def test_downgrade_removes_cdc_objects_and_grants(app_database):
    url = app_database("cdc_downgrade")
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "0001")

    leftovers = asyncio.run(
        query(
            url,
            "SELECT (SELECT count(*) FROM pg_publication WHERE pubname = 'shop_cdc'),"
            " (SELECT count(*) FROM pg_namespace WHERE nspname = 'meta'),"
            " to_regclass('public.heartbeat') IS NULL,"
            " has_table_privilege('debezium', 'orders', 'SELECT'),"
            " (SELECT count(*) FROM pg_namespace, aclexplode(nspacl) acl"
            "   WHERE nspname = 'public' AND acl.grantee = 'debezium'::regrole)",
        )
    )
    assert leftovers == [(0, 0, True, False, 0)]

    command.upgrade(config, "head")
    command.check(config)


async def test_debezium_privileges_are_exactly_the_spec(database_url):
    tables = await query(
        database_url,
        "SELECT n.nspname || '.' || c.relname, acl.privilege_type"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) acl"
        " WHERE acl.grantee = 'debezium'::regrole ORDER BY 1, 2",
    )
    schemas = await query(
        database_url,
        "SELECT n.nspname, acl.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) acl"
        " WHERE acl.grantee = 'debezium'::regrole ORDER BY 1, 2",
    )
    expected = sorted([(f"public.{table}", "SELECT") for table in PUBLISHED] + [("public.heartbeat", "UPDATE")])
    assert tables == expected
    assert schemas == [("public", "USAGE")]
