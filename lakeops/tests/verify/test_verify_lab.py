"""verify against real snapshots on the lab stack (`make lab-test`): an append, a duplicate key, an OPTIMIZE and a
DELETE, each found by its own query id. Tables are left for the stack's reset (a drop would make Polaris purge)."""

import time

import pytest
from lab_trino import connect

from lakeops.verify import TableKind, verify

pytestmark = pytest.mark.lab

KEYED = TableKind(key_columns=("id",))


def run(conn, statement: str) -> str:
    cur = conn.cursor()
    cur.execute(statement)
    cur.fetchall()
    return cur.query_id


def test_verify_finds_each_operation_by_its_query_id_and_checks_it(lab):
    conn = connect("harness")
    table = f"lab_exp.lakeops_verify.t_{time.time_ns()}"
    run(conn, "CREATE SCHEMA IF NOT EXISTS lab_exp.lakeops_verify")
    run(conn, f"CREATE TABLE {table} (id bigint, v bigint) WITH (format_version = 2)")

    first = verify(
        conn, table, query_id=run(conn, f"INSERT INTO {table} VALUES (1, 1), (2, 2)"), op="append", kind=KEYED
    )
    assert first.ok, first.findings
    second = run(conn, f"INSERT INTO {table} VALUES (3, 3)")
    appended = verify(conn, table, query_id=second, op="append", kind=KEYED)
    assert appended.ok, appended.findings
    assert appended.rollback_safe, appended.rollback_reason

    duplicate = verify(conn, table, query_id=run(conn, f"INSERT INTO {table} VALUES (1, 9)"), op="append", kind=KEYED)
    assert [f.check for f in duplicate.findings] == ["keys"]

    rewrite = verify(conn, table, query_id=run(conn, f"ALTER TABLE {table} EXECUTE optimize"), op="rewrite", kind=KEYED)
    assert [f.check for f in rewrite.findings] == ["keys"]  # the duplicate is still there; the rows are unchanged
    assert not rewrite.rollback_safe

    deleted = run(conn, f"DELETE FROM {table} WHERE v = 9")
    assert verify(conn, table, query_id=deleted, op="dml", kind=KEYED, effect_bounds=(-1, 0)).ok
    assert not verify(conn, table, query_id=deleted, op="dml", kind=KEYED, effect_bounds=(0, 0)).ok
    assert not verify(conn, table, query_id=second, op="append", kind=KEYED).rollback_safe  # commits came after it
