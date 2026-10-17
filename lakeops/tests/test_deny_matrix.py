"""Deny matrix of the lab's safety boundary: identity x catalog x operation, run against the lab stack.

The boundary is the catalog (docs/adr/0417-catalog-scoped-safety-boundary.md):
- an identity reaches only its catalogs; every other catalog is `allow: none`, so even table procedures fail there;
- a baseline run's catalog lab_base_<run> (lab/stack.sh base <run>; the active run is in $LAB_RUN_DIR/base/active) is
  baseline_writer's alone and holds only that run's warehouse;
- Polaris refuses a table located outside the catalog's own warehouse, so no writer reaches another zone's or run's
  files by naming a location;
- agent_planner's lab_ro is read only in the engine (iceberg.security=READ_ONLY), the one Trino layer that also
  refuses table procedures (ALTER TABLE ... EXECUTE); file-based rules alone do not;
- no identity may set the *_min_retention session properties, and the floors of lab_ops, lab_sbx, lab_ro and the
  baseline run (7 simulated days, 7m at the published time scale) reject shorter procedure thresholds;
- CALL procedures are refused for everyone (no `procedures` rule), and Polaris refuses Trino's DROP TABLE (a purge)
  on the evaluation warehouse, where drop-with-purge stays off;
- an identity sees and cancels only its own queries (`queries` rule): without it, any identity could read another's
  SQL in system.runtime.queries and kill it through the REST API.
Each case gets a fresh table (CREATE + 2 INSERTs = 3 snapshots), created by the zone's writer.
"""

import time
import uuid

import pytest
import trino
from lab_trino import RUN, connect, query, rest

pytestmark = pytest.mark.lab

ACTIVE_RUN = (RUN / "base/active").read_text().strip() if (RUN / "base/active").is_file() else None
BASE = f"lab_base_{ACTIVE_RUN}" if ACTIVE_RUN else None

IDENTITIES = ["harness", "agent_planner", "executor", "baseline_writer"]
CATALOGS = ["lab_exp", "lab_ro", "lab_ops", "lab_sbx", *([BASE] if BASE else [])]
# (identity, catalog) -> access; any pair not listed has none.
ACCESS = {
    ("harness", "lab_exp"): "write",
    ("agent_planner", "lab_ro"): "read",
    ("executor", "lab_ops"): "write",
    ("executor", "lab_sbx"): "write",
    ("baseline_writer", BASE): "write",
}
# catalog -> (identity, catalog) that creates its probe tables; lab_ro reads the evaluation warehouse of lab_ops.
WRITER = {
    "lab_exp": ("harness", "lab_exp"),
    "lab_ro": ("executor", "lab_ops"),
    "lab_ops": ("executor", "lab_ops"),
    "lab_sbx": ("executor", "lab_sbx"),
    BASE: ("baseline_writer", BASE),
}
FLOOR = {"lab_exp": "0s", "lab_ro": "7m", "lab_ops": "7m", "lab_sbx": "7m", BASE: "7m"}
DROP_WITH_PURGE = {"lab_exp": True, "lab_ro": False, "lab_ops": False, "lab_sbx": True, BASE: True}
READ_ONLY = {"lab_ro"}  # iceberg.security=READ_ONLY
# Outside every catalog's warehouse (each is s3://<bucket>/warehouse or s3://lab-base/<run>/warehouse), shaped like
# another baseline run's warehouse: the bucket key reaches it, only Polaris's location check stands in the way.
ELSEWHERE = "s3://lab-base/another_run/warehouse"

OPERATIONS = {
    "select": "SELECT count(*) FROM {t}",
    "insert": "INSERT INTO {t} VALUES (9)",
    "delete": "DELETE FROM {t} WHERE id = 1",
    "set_properties": "ALTER TABLE {t} SET PROPERTIES format = 'ORC'",
    "create_table": "CREATE TABLE {c}.deny_matrix.{name}_new (id bigint) WITH (format_version = 2)",
    "create_table_elsewhere": (
        "CREATE TABLE {c}.deny_matrix.{name}_far (id bigint) WITH (format_version = 2, location = '{elsewhere}/{name}')"
    ),
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
    if op == "create_table_elsewhere":
        return "outside"
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
    # Polaris refuses a table location outside the catalog's warehouse with 403 "... is not in the list of allowed
    # locations" (seen through the REST API on the lab, 2026-10-18); Trino reports only this.
    if error.error_name == "ICEBERG_CATALOG_ERROR" and error.message == "Failed to create transaction":
        return "outside"
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
        t=f"{catalog}.deny_matrix.{name}",
        c=catalog,
        name=name,
        floor=FLOOR[catalog],
        first_insert=first_insert,
        elsewhere=ELSEWHERE,
    )
    try:
        query(connect(identity), sql)
        error = None
    except trino.exceptions.TrinoQueryError as e:
        error = e
    assert outcome(error) == want, sql


def test_a_baseline_run_is_active(lab) -> None:
    """`make lab-test` activates run `matrix` first; without an active run the matrix has no lab_base_<run> cases."""
    assert BASE, f"no active baseline run in {RUN}/base/active: lab/stack.sh base matrix"


def test_each_identity_sees_only_its_catalogs(lab) -> None:
    seen = {identity: {row[0] for row in query(connect(identity), "SHOW CATALOGS")} for identity in IDENTITIES}
    assert seen == {
        "harness": {"lab_exp", "system"},
        "agent_planner": {"lab_ro", "system"},
        "executor": {"lab_ops", "lab_sbx", "system"},
        "baseline_writer": {BASE, "system"} if BASE else {"system"},
    }


# A query that stays RUNNING while its client does not fetch: 10^8 rows fill the output buffer and Trino waits.
SLOW = "SELECT x FROM UNNEST(sequence(1, 10000)) a(x) CROSS JOIN UNNEST(sequence(1, 10000)) b(y)"


def state(user: str, query_id: str) -> str | None:
    rows = query(connect(user), f"SELECT state FROM system.runtime.queries WHERE query_id = '{query_id}'")
    return rows[0][0] if rows else None


@pytest.fixture
def running_query(lab):
    """start(owner) -> id of a RUNNING query of `owner`; every query started is cancelled afterwards."""
    started = []

    def start(owner: str) -> str:
        cur = connect(owner).cursor()
        cur.execute(SLOW)
        started.append((owner, cur.query_id))
        deadline = time.monotonic() + 30
        while state(owner, cur.query_id) != "RUNNING":
            assert time.monotonic() < deadline, f"query {cur.query_id} did not reach RUNNING"
            time.sleep(0.5)
        return cur.query_id

    yield start
    for owner, query_id in started:
        rest(owner, "DELETE", f"/v1/query/{query_id}")


@pytest.mark.parametrize(("viewer", "owner"), [(v, o) for v in IDENTITIES for o in IDENTITIES if v != o])
def test_another_identity_cannot_see_or_cancel_a_query(running_query, viewer: str, owner: str) -> None:
    query_id = running_query(owner)
    seen = query(connect(viewer), f"SELECT count(*) FROM system.runtime.queries WHERE query_id = '{query_id}'")
    assert seen == [[0]]
    assert rest(viewer, "GET", f"/v1/query/{query_id}").status_code == 403
    assert rest(viewer, "DELETE", f"/v1/query/{query_id}").status_code == 403
    assert state(owner, query_id) == "RUNNING"
    # The owner still can (control).
    assert rest(owner, "GET", f"/v1/query/{query_id}").status_code == 200
    assert rest(owner, "DELETE", f"/v1/query/{query_id}").status_code == 204


@pytest.mark.parametrize("identity", IDENTITIES)
def test_each_identity_sees_only_its_own_queries(lab, identity: str) -> None:
    for other in IDENTITIES:  # every identity has a query in the history
        query(connect(other), "SELECT 1")
    users = {row[0] for row in query(connect(identity), 'SELECT DISTINCT "user" FROM system.runtime.queries')}
    assert users == {identity}
