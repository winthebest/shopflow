"""lakeops on the lab stack, as `harness` in lab_exp: clone keeps the layout, rollback restores the fingerprint,
features describe a fragmented table and its history."""

import re
import uuid

import pytest
from lab_trino import connect, query

from lakeops.features import features
from lakeops.fingerprint import checksum_at, fingerprint
from lakeops.snapshots import clone, current_snapshot_id, refs, rollback, snapshots, table_properties
from lakeops.sql import TableName
from lakeops.state import capture, capture_namespace

pytestmark = pytest.mark.lab
SCHEMA = "lakeops_probe"


@pytest.fixture(scope="module")
def conn(lab):
    c = connect("harness")
    query(c, f"CREATE SCHEMA IF NOT EXISTS lab_exp.{SCHEMA}")
    return c


@pytest.fixture
def table(conn):
    """A partitioned, sorted ORC table with 3 inserts (4 snapshots, CREATE's empty one included), dropped afterwards."""
    t = TableName("lab_exp", SCHEMA, f"t_{uuid.uuid4().hex[:10]}")
    query(
        conn,
        f"CREATE TABLE {t.sql} (id bigint, day date, v varchar) WITH (format_version = 2, format = 'ORC', "
        "partitioning = ARRAY['day', 'bucket(id, 4)'], sorted_by = ARRAY['id'])",
    )
    for i in range(3):
        query(
            conn,
            f"INSERT INTO {t.sql} VALUES ({i}, DATE '2026-10-0{i + 1}', 'v{i}'), ({i + 10}, DATE '2026-10-01', 'w')",
        )
    yield t
    for name in (t, TableName(t.catalog, t.schema, t.table + "_clone")):
        query(conn, f"DROP TABLE IF EXISTS {name.sql}")


def layout(conn, t: TableName) -> dict[str, str]:
    shown = query(conn, f"SHOW CREATE TABLE {t.sql}")[0][0]
    return dict(re.findall(r"^\s+(\w+) = (.+?),?$", shown, re.MULTILINE))


def test_a_clone_keeps_the_layout_and_rows_in_its_own_location(conn, table):
    copy = clone(conn, table, f"{table}_clone")
    source, cloned = layout(conn, table), layout(conn, copy)
    for key in ("format", "format_version", "partitioning", "sorted_by"):
        assert cloned[key] == source[key], key
    assert cloned["location"] != source["location"]
    assert fingerprint(conn, copy).same_rows(fingerprint(conn, table))
    assert len(snapshots(conn, copy)) <= 2  # the clone has the rows, not the history


def test_a_rollback_restores_the_fingerprint(conn, table):
    before, snapshot = fingerprint(conn, table), current_snapshot_id(conn, table)
    query(conn, f"DELETE FROM {table.sql} WHERE id = 1")
    query(conn, f"INSERT INTO {table.sql} VALUES (99, DATE '2026-10-09', 'x')")
    changed = fingerprint(conn, table)
    assert not changed.same_rows(before)
    rollback(conn, table, snapshot)
    after = fingerprint(conn, table)
    assert after.same_state(before)
    assert after.snapshots == before.snapshots + 2  # the history keeps the undone commits
    assert current_snapshot_id(conn, table) == snapshot
    assert [r.name for r in refs(conn, table)] == ["main"]


def test_features_of_a_fragmented_table_and_its_history(conn, table):
    f = features(conn, table)
    # 2 rows per insert over day x bucket(id, 4): 5 files (ids 0 and 10 of 2026-10-01 share a bucket), 4 partitions.
    assert f.data_files >= 5 and f.partitions >= 3
    assert f.small_file_ratio == 1.0 and f.snapshots == 4
    assert f.history_bytes == 0 and f.ref_pinned_bytes == 0
    query(conn, f"DELETE FROM {table.sql} WHERE id = 10")
    assert features(conn, table).position_delete_files >= 1
    query(conn, f"ALTER TABLE {table.sql} EXECUTE optimize")
    compacted = features(conn, table)
    assert compacted.data_files < f.data_files and compacted.position_delete_files == 0
    assert compacted.history_bytes > 0  # the files optimize replaced stay referenced by older snapshots


def test_a_capture_shows_which_partition_changed(conn, table):
    before = capture(conn, table)
    assert before.rows == 6 and len(before.snapshots) == 4 and [r.name for r in before.refs] == ["main"]
    keys = [p.partition for p in before.partitions]
    assert all(re.fullmatch(r"day=[^/]+/id_bucket=\d+", k) for k in keys), keys  # Iceberg's path, spec order
    query(conn, f"INSERT INTO {table.sql} VALUES (0, DATE '2026-10-01', 'again')")
    after = capture(conn, table)
    changed = {p.partition for p in after.partitions} ^ {p.partition for p in before.partitions} | {
        a.partition for a in after.partitions for b in before.partitions if a.partition == b.partition and a != b
    }
    assert len(changed) == 1 and after.rows == 7
    assert len(after.snapshots) == 5 and len(after.files) == len(before.files) + 1
    assert capture_namespace(conn, table.catalog, table.schema)[table.table] == after
    assert after.columns == (("id", "bigint"), ("day", "date"), ("v", "varchar"))
    query(conn, f"ALTER TABLE {table.sql} ADD COLUMN note varchar")
    query(conn, f"ALTER TABLE {table.sql} SET PROPERTIES format = 'PARQUET'")
    altered = capture(conn, table)
    assert altered.rows == after.rows and altered.columns[-1] == ("note", "varchar")
    assert altered.definition_sha256 != after.definition_sha256 and altered.properties != after.properties


def test_an_unpartitioned_table_is_one_partition(conn):
    t = TableName("lab_exp", SCHEMA, f"u_{uuid.uuid4().hex[:10]}")
    query(conn, f"CREATE TABLE {t.sql} WITH (format_version = 2) AS SELECT 1 AS id")
    try:
        assert [(p.partition, p.rows) for p in capture(conn, t).partitions] == [("", 1)]
        f = features(conn, t)
        assert (f.partitions, f.data_files, f.max_files_per_partition) == (1, 1, 1)
    finally:
        query(conn, f"DROP TABLE {t.sql}")


def test_the_properties_of_show_create_table(conn, table):
    properties = table_properties(conn, table)
    assert properties["format"] == "'ORC'" and properties["format_version"] == "2"
    assert properties["partitioning"] == "ARRAY['day','bucket(id, 4)']"
    assert properties["location"].startswith("'s3://lab-exp/warehouse/lakeops_probe/")


def test_a_checksum_at_an_older_snapshot_matches_the_state_then(conn, table):
    before, snapshot = fingerprint(conn, table), current_snapshot_id(conn, table)
    query(conn, f"DELETE FROM {table.sql} WHERE id = 2")
    assert checksum_at(conn, table, snapshot) == (before.rows, before.checksum)
    assert checksum_at(conn, table) != (before.rows, before.checksum)
