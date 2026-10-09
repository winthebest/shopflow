"""Migration 0003: read-only role for the Trino catalog `pg` (docs/contracts/services.md, "Other database roles")."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from orders.db import create_engine
from orders.migrate import alembic_config, upgrade

pytestmark = pytest.mark.integration

READABLE = ["customers", "products", "orders", "order_items", "payments", "heartbeat", "meta.cdc_epochs"]

# Every privilege trino_pg holds, read from the ACLs themselves: "nothing else" is checked, not assumed.
TABLE_PRIVILEGES = """
    SELECT n.nspname || '.' || c.relname, acl.privilege_type
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) acl
    WHERE acl.grantee = 'trino_pg'::regrole
    ORDER BY 1, 2
"""
SCHEMA_PRIVILEGES = """
    SELECT n.nspname, acl.privilege_type
    FROM pg_namespace n, aclexplode(n.nspacl) acl
    WHERE acl.grantee = 'trino_pg'::regrole
    ORDER BY 1, 2
"""


async def query(url: str, sql: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            return [tuple(row) for row in await conn.execute(text(sql))]
    finally:
        await engine.dispose()


async def test_trino_pg_is_a_plain_login_role(database_url):
    rows = await query(
        database_url,
        "SELECT rolcanlogin, rolsuper, rolreplication, rolcreatedb, rolcreaterole FROM pg_roles"
        " WHERE rolname = 'trino_pg'",
    )
    assert rows == [(True, False, False, False, False)]


async def test_trino_pg_privileges_are_exactly_the_spec(database_url):
    """SELECT on every published source table plus meta.cdc_epochs, USAGE on their schemas, nothing else.

    Derived from the publication, so a new source table whose migration forgets the trino_pg grant fails here.
    """
    published = await query(
        database_url, "SELECT schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = 'shop_cdc'"
    )
    tables = await query(database_url, TABLE_PRIVILEGES)
    schemas = await query(database_url, SCHEMA_PRIVILEGES)

    expected = sorted([(name, "SELECT") for (name,) in published] + [("meta.cdc_epochs", "SELECT")])
    assert expected == sorted((name if "." in name else f"public.{name}", "SELECT") for name in READABLE)
    assert tables == expected
    assert schemas == [("meta", "USAGE"), ("public", "USAGE")]


@pytest.mark.parametrize("relation", READABLE)
async def test_trino_pg_can_read(trino_url, relation):
    await query(trino_url, f"SELECT * FROM {relation} LIMIT 1")  # noqa: S608 - fixed relation names


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders (customer_id, status, total) VALUES (1, 'pending', 1)",
        "UPDATE heartbeat SET beat_at = now()",
        "DELETE FROM payments",
        "TRUNCATE orders",
        "INSERT INTO meta.cdc_epochs (epoch) VALUES (99)",
        "CREATE TABLE public.sneaky (id int)",
        "CREATE TABLE meta.sneaky (id int)",
        "SELECT * FROM alembic_version",
    ],
    ids=[
        "insert-orders",
        "update-heartbeat",
        "delete-payments",
        "truncate-orders",
        "insert-cdc-epochs",
        "create-in-public",
        "create-in-meta",
        "read-alembic-version",
    ],
)
async def test_trino_pg_cannot_write_create_or_read_anything_else(trino_url, sql):
    with pytest.raises(DBAPIError, match="permission denied"):
        await query(trino_url, sql)


def test_migration_fails_clearly_when_trino_pg_is_missing(app_database, superuser_sql):
    url = app_database("no_trino_role")
    superuser_sql("ALTER ROLE trino_pg RENAME TO trino_pg_away")
    try:
        with pytest.raises(DBAPIError, match='Trino role "trino_pg" is missing: create it before migrating'):
            upgrade(url)
    finally:
        superuser_sql("ALTER ROLE trino_pg_away RENAME TO trino_pg")
    # One transaction for the whole upgrade: nothing from 0001-0003 is left half-applied.
    assert asyncio.run(query(url, "SELECT to_regclass('public.alembic_version') IS NULL")) == [(True,)]


def test_downgrade_revokes_every_trino_pg_privilege(app_database):
    url = app_database("trino_downgrade")
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "0002")

    assert asyncio.run(query(url, TABLE_PRIVILEGES)) == []
    assert asyncio.run(query(url, SCHEMA_PRIVILEGES)) == []

    command.upgrade(config, "head")
    command.check(config)
