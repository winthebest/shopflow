"""Migration 0004: `shipments` source table and the fulfillment_worker role (docs/contracts/services.md)."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from orders.db import create_engine
from orders.migrate import alembic_config, upgrade

pytestmark = pytest.mark.integration

# Every privilege fulfillment_worker holds, read from the ACLs (table, column and schema level).
TABLE_PRIVILEGES = """
    SELECT n.nspname || '.' || c.relname, acl.privilege_type
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) acl
    WHERE acl.grantee = 'fulfillment_worker'::regrole ORDER BY 1, 2
"""
COLUMN_PRIVILEGES = """
    SELECT n.nspname || '.' || c.relname || '.' || a.attname, acl.privilege_type
    FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace,
         aclexplode(a.attacl) acl
    WHERE acl.grantee = 'fulfillment_worker'::regrole ORDER BY 1, 2
"""
SCHEMA_PRIVILEGES = """
    SELECT n.nspname, acl.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) acl
    WHERE acl.grantee = 'fulfillment_worker'::regrole ORDER BY 1, 2
"""
# What the worker runs (fulfillment-worker): idempotent on order_id.
INSERT_SHIPMENT = """
    INSERT INTO shipments (order_id, cdc_epoch, source_lsn) VALUES ({order_id}, 7, 1000)
    ON CONFLICT (order_id) DO NOTHING
"""


async def execute(url: str, sql: str) -> tuple[int, list[tuple]]:
    """(rowcount, rows) of one statement in its own transaction."""
    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql))
            rows = [tuple(row) for row in result] if result.returns_rows else []
            return result.rowcount, rows
    finally:
        await engine.dispose()


async def an_order(url: str) -> int:
    _, rows = await execute(
        url, "INSERT INTO orders (customer_id, status, total) VALUES (1, 'paid', 9.90) RETURNING id"
    )
    return rows[0][0]


async def test_worker_privileges_are_exactly_the_spec(database_url):
    assert (await execute(database_url, TABLE_PRIVILEGES))[1] == [("public.shipments", "INSERT")]
    assert (await execute(database_url, COLUMN_PRIVILEGES))[1] == [("public.orders.id", "SELECT")]
    assert (await execute(database_url, SCHEMA_PRIVILEGES))[1] == [("public", "USAGE")]


async def test_worker_role_is_a_plain_login_role(database_url):
    _, rows = await execute(
        database_url,
        "SELECT rolcanlogin, rolsuper, rolreplication, rolcreatedb, rolcreaterole FROM pg_roles"
        " WHERE rolname = 'fulfillment_worker'",
    )
    assert rows == [(True, False, False, False, False)]


async def test_worker_creates_one_shipment_per_order_idempotently(seeded_db, worker_url):
    order_id = await an_order(seeded_db)

    first, _ = await execute(worker_url, INSERT_SHIPMENT.format(order_id=order_id))
    replay, _ = await execute(worker_url, INSERT_SHIPMENT.format(order_id=order_id))  # re-delivery / re-snapshot

    assert (first, replay) == (1, 0)  # ON CONFLICT DO NOTHING needs no privilege beyond INSERT
    _, rows = await execute(
        seeded_db, f"SELECT order_id, cdc_epoch, source_lsn FROM shipments WHERE order_id = {order_id}"
    )
    assert rows == [(order_id, 7, 1000)]


async def test_worker_can_check_which_orders_exist(seeded_db, worker_url):
    order_id = await an_order(seeded_db)
    _, rows = await execute(worker_url, f"SELECT id FROM orders WHERE id IN ({order_id}, 999999)")
    assert rows == [(order_id,)]


async def test_shipment_for_unknown_order_is_rejected(seeded_db, worker_url):
    with pytest.raises(DBAPIError, match="fk_shipments_order_id_orders"):
        await execute(worker_url, INSERT_SHIPMENT.format(order_id=999999))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT total FROM orders",
        "SELECT * FROM shipments",
        "UPDATE shipments SET cdc_epoch = 8",
        "DELETE FROM shipments",
        "INSERT INTO orders (customer_id, status, total) VALUES (1, 'paid', 1)",
        "SELECT * FROM customers",
        "CREATE TABLE public.sneaky (id int)",
    ],
    ids=[
        "read-order-total",
        "read-shipments",
        "update-shipments",
        "delete-shipments",
        "insert-orders",
        "read-customers",
        "create-table",
    ],
)
async def test_worker_cannot_do_anything_else(seeded_db, worker_url, sql):
    with pytest.raises(DBAPIError, match="permission denied"):
        await execute(worker_url, sql)


async def test_shipments_updated_at_trigger(seeded_db, worker_url):
    order_id = await an_order(seeded_db)
    await execute(worker_url, INSERT_SHIPMENT.format(order_id=order_id))
    _, before = await execute(seeded_db, f"SELECT updated_at FROM shipments WHERE order_id = {order_id}")
    await execute(seeded_db, f"UPDATE shipments SET cdc_epoch = 8 WHERE order_id = {order_id}")
    _, after = await execute(seeded_db, f"SELECT updated_at FROM shipments WHERE order_id = {order_id}")
    assert after[0][0] > before[0][0]


def test_migration_fails_clearly_when_worker_role_is_missing(app_database, superuser_sql):
    url = app_database("no_worker_role")
    superuser_sql("ALTER ROLE fulfillment_worker RENAME TO fulfillment_worker_away")
    try:
        with pytest.raises(DBAPIError, match='Worker role "fulfillment_worker" is missing: create it before migrating'):
            upgrade(url)
    finally:
        superuser_sql("ALTER ROLE fulfillment_worker_away RENAME TO fulfillment_worker")
    _, rows = asyncio.run(execute(url, "SELECT to_regclass('public.alembic_version') IS NULL"))
    assert rows == [(True,)]  # one transaction for the whole upgrade


def test_downgrade_removes_shipments_and_worker_grants(app_database):
    url = app_database("shipments_downgrade")
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "0003")

    _, leftovers = asyncio.run(
        execute(
            url,
            "SELECT to_regclass('public.shipments') IS NULL,"
            " (SELECT count(*) FROM pg_publication_tables WHERE pubname = 'shop_cdc' AND tablename = 'shipments')",
        )
    )
    assert leftovers == [(True, 0)]
    for inventory in (TABLE_PRIVILEGES, COLUMN_PRIVILEGES, SCHEMA_PRIVILEGES):
        assert asyncio.run(execute(url, inventory))[1] == []

    command.upgrade(config, "head")
    command.check(config)
