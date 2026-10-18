"""verify_state on real captures in the lab stack (`make lab-test`): a rollback, an added column and an expire, each
checked against its plan from two `lakeops.state` captures. Tables are left for the stack's reset."""

import time

import pytest
from lab_trino import connect

from lakeops.fingerprint import checksum_at
from lakeops.state import capture
from lakeops.verify import ExpectDefinition, ExpectExpire, ExpectRollback, verify_state

pytestmark = pytest.mark.lab


def run(conn, statement: str) -> None:
    cur = conn.cursor()
    cur.execute(statement)
    cur.fetchall()


def test_rollback_add_column_and_expire_match_their_plans(lab):
    conn = connect("harness")
    table = f"lab_exp.lakeops_verify.s_{time.time_ns()}"
    run(conn, "CREATE SCHEMA IF NOT EXISTS lab_exp.lakeops_verify")
    run(conn, f"CREATE TABLE {table} (id bigint, v varchar) WITH (format_version = 2)")
    for i in range(3):
        run(conn, f"INSERT INTO {table} VALUES ({i}, 'v{i}')")
    snapshots = [s.snapshot_id for s in capture(conn, table).snapshots]
    target = snapshots[-2]

    before = capture(conn, table)
    rows, checksum = checksum_at(conn, table, target)
    run(conn, f"ALTER TABLE {table} EXECUTE rollback_to_snapshot({target})")
    rollback = verify_state(before, capture(conn, table), op="history", expected=ExpectRollback(target, rows, checksum))
    assert rollback.ok, rollback.findings

    before = capture(conn, table)
    run(conn, f"ALTER TABLE {table} ADD COLUMN note varchar")
    added = verify_state(
        before, capture(conn, table), op="definition", expected=ExpectDefinition(columns_added=(("note", "varchar"),))
    )
    assert added.ok, added.findings

    before = capture(conn, table)
    planned = frozenset(snapshots) - {target}
    run(conn, f"ALTER TABLE {table} EXECUTE expire_snapshots(retention_threshold => '0s')")
    expired = verify_state(before, capture(conn, table), op="history", expected=ExpectExpire(planned))
    assert expired.ok, expired.findings
    assert not expired.rollback_safe
