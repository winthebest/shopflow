"""lakeops.state without a stack: a captured state survives JSON."""

import json

from lakeops.state import FileEntry, PartitionState, RefEntry, SnapshotEntry, TableState, partition_key

STATE = TableState(
    table="lab_ops.lab_eval_t1.bronze_orders",
    rows=3,
    checksum="0a1b",
    partitions=(PartitionState("_ingested_at_day=2026-01-01", 3, "0a1b"),),
    snapshots=(SnapshotEntry(11, None, "2026-10-18T01:00:00+00:00", "append"),),
    refs=(RefEntry("main", "BRANCH", 11),),
    files=(FileEntry("s3://lab-evalprod/warehouse/x.parquet", 0, 3, 812),),
    columns=(("id", "bigint"), ("status", "varchar")),
    properties=(("write.format.default", "PARQUET"),),
    definition_sha256="ab" * 32,
)


def test_a_state_round_trips_through_json():
    assert TableState.from_json(json.loads(json.dumps(STATE.to_json()))) == STATE


def test_an_unpartitioned_table_has_the_empty_key():
    assert partition_key(None) == ""
