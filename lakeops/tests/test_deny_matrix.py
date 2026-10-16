"""Deny matrix of the lab's safety boundary: identity x catalog x operation, run against the lab stack.

The boundary is the catalog (docs/adr/0417-catalog-scoped-safety-boundary.md):
- an identity reaches only its catalogs; every other catalog is `allow: none`, so even table procedures fail there;
- agent_planner's lab_ro is read only in the engine (iceberg.security=READ_ONLY), the one Trino layer that also
  refuses table procedures (ALTER TABLE ... EXECUTE); file-based rules alone do not;
- no identity may set the *_min_retention session properties, and the 7d floors of lab_ops, lab_sbx and lab_ro
  reject shorter procedure thresholds;
- CALL procedures are refused for everyone (no `procedures` rule), and Polaris refuses Trino's DROP TABLE (a purge)
  on the evaluation warehouse, where drop-with-purge stays off.
Each case gets a fresh table (CREATE + 2 INSERTs = 3 snapshots), created by the zone's writer.
"""

import uuid

import pytest
import trino
from lab_trino import connect, query

pytestmark = pytest.mark.lab

IDENTITIES = ["harness", "agent_planner", "executor"]
CATALOGS = ["lab_exp", "lab_ro", "lab_ops", "lab_sbx"]
# (identity, catalog) -> access; any pair not listed has none.
ACCESS = {
    ("harness", "lab_exp"): "write",
    ("agent_planner", "lab_ro"): "read",
    ("executor", "lab_ops"): "write",
    ("executor", "lab_sbx"): "write",
}
# catalog -> (identity, catalog) that creates its probe tables; lab_ro reads the evaluation warehouse of lab_ops.
WRITER = {
    "lab_exp": ("harness", "lab_exp"),
    "lab_ro": ("executor", "lab_ops"),
    "lab_ops": ("executor", "lab_ops"),
    "lab_sbx": ("executor", "lab_sbx"),
}
FLOOR = {"lab_exp": "0s", "lab_ro": "7d", "lab_ops": "7d", "lab_sbx": "7d"}
DROP_WITH_PURGE = {"lab_exp": True, "lab_ro": False, "lab_ops": False, "lab_sbx": True}
READ_ONLY = {"lab_ro"}  # iceberg.security=READ_ONLY

OPERATIONS = {
    "select": "SELECT count(*) FROM {t}",
    "insert": "INSERT INTO {t} VALUES (9)",
    "delete": "DELETE FROM {t} WHERE id = 1",
    "set_properties": "ALTER TABLE {t} SET PROPERTIES format = 'ORC'",
    "create_table": "CREATE TABLE {c}.deny_matrix.{name}_new (id bigint) WITH (format_version = 2)",
    "drop_table": "DROP TABLE {t}",
    "optimize": "ALTER TABLE {t} EXECUTE optimize",
    "expire_snapshots": "ALTER TABLE {t} EXECUTE expire_snapshots(retention_threshold => '{floor}')",
    "expire_snapshots_below_floor": "ALTER TABLE {t} EXECUTE expire_snapshots(retention_threshold => '0s')",
    "remove_orphan_files": "ALTER TABLE {t} EXECUTE remove_orphan_files(retention_threshold => '{floor}')",
    "remove_orphan_files_below_floor": "ALTER TABLE {t} EXECUTE remove_orphan_files(retention_threshold => '0s')",
    "rollback_to_snapshot": "ALTER TABLE {t} EXECUTE rollback_to_snapshot(snapshot_id => {first_insert})",
    "call_unregister_table": "CALL {c}.system.unregister_table(schema_name => 'deny_matrix', table_name => '{name}')",
    "set_session_expire_floor": "SET SESSION {c}.expire_snapshots_min_retention = '0s'",
    "set_session_orphan_floor": "SET SESSION {c}.remove_orphan_files_min_retention = '0s'",
}
NEEDS_TABLE = {op for op, sql in OPERATIONS.items() if "{t}" in sql or "{name}" in sql}
TABLE_PROCEDURES = {op for op, sql in OPERATIONS.items() if " EXECUTE " in sql}


def expected(identity: str, catalog: str, op: str) -> str:
    access = ACCESS.get((identity, catalog))
    if access is None:
        # A READ_ONLY connector refuses a table procedure before Trino checks catalog access; either way it is refused.
        return "denied" if catalog in READ_ONLY and op in TABLE_PROCEDURES else "no catalog"
    if op.startswith("set_session_") or op == "call_unregister_table":
        return "denied"
    if access == "read":
        return "ok" if op == "select" else "denied"
    if op.endswith("_below_floor") and FLOOR[catalog] != "0s":
        return "floor"
    if op == "drop_table" and not DROP_WITH_PURGE[catalog]:
        return "no purge"
    return "ok"


def outcome(error: trino.exceptions.TrinoQueryError | None) -> str:
    if error is None:
        return "ok"
    if error.error_name == "PERMISSION_DENIED":
        return "no catalog" if error.message.startswith("Access Denied: Cannot access catalog") else "denied"
    if "is shorter than the minimum retention configured in the system" in error.message:
        return "floor"
    if error.error_name == "ICEBERG_CATALOG_ERROR" and error.message.startswith("Failed to drop table"):
        return "no purge"
    return f"unexpected {error.error_name}: {error.message}"


@pytest.fixture(scope="module")
def namespaces(lab) -> None:
    for identity, catalog in set(WRITER.values()):
        query(connect(identity), f"CREATE SCHEMA IF NOT EXISTS {catalog}.deny_matrix")


def probe_table(catalog: str) -> tuple[str, int]:
    """A fresh table in the warehouse behind `catalog`, created by its writer; returns its name and first insert."""
    identity, writer_catalog = WRITER[catalog]
    conn = connect(identity)
    name = f"m_{uuid.uuid4().hex[:12]}"
    fq = f"{writer_catalog}.deny_matrix.{name}"
    query(conn, f"CREATE TABLE {fq} (id bigint) WITH (format_version = 2)")
    query(conn, f"INSERT INTO {fq} VALUES (1), (2)")
    query(conn, f"INSERT INTO {fq} VALUES (3)")
    snapshots = query(
        conn, f'SELECT snapshot_id FROM {writer_catalog}.deny_matrix."{name}$snapshots" ORDER BY committed_at'
    )
    return name, snapshots[1][0]  # snapshots[0] is CREATE TABLE's empty snapshot


@pytest.mark.parametrize("op", OPERATIONS)
@pytest.mark.parametrize("catalog", CATALOGS)
@pytest.mark.parametrize("identity", IDENTITIES)
def test_deny_matrix(namespaces, identity: str, catalog: str, op: str) -> None:
    want = expected(identity, catalog, op)
    # Always an existing table: for a missing one Trino reports "does not exist" before it checks catalog access.
    name, first_insert = probe_table(catalog) if op in NEEDS_TABLE else ("unused", 0)
    sql = OPERATIONS[op].format(
        t=f"{catalog}.deny_matrix.{name}", c=catalog, name=name, floor=FLOOR[catalog], first_insert=first_insert
    )
    try:
        query(connect(identity), sql)
        error = None
    except trino.exceptions.TrinoQueryError as e:
        error = e
    assert outcome(error) == want, sql


def test_each_identity_sees_only_its_catalogs(lab) -> None:
    seen = {identity: {row[0] for row in query(connect(identity), "SHOW CATALOGS")} for identity in IDENTITIES}
    assert seen == {
        "harness": {"lab_exp", "system"},
        "agent_planner": {"lab_ro", "system"},
        "executor": {"lab_ops", "lab_sbx", "system"},
    }
