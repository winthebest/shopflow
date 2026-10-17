"""The table states and a prodlike namespace on the lab stack."""

import uuid
from datetime import timedelta
from itertools import pairwise

import pytest

from lab import tables
from lab.guard import guarded_connect
from lab.prodlike import prodlike, table
from lab.trino_client import Settings, query
from lakeops.features import features
from lakeops.fingerprint import fingerprint
from lakeops.snapshots import snapshots
from lakeops.sql import TableName

pytestmark = pytest.mark.lab


def test_a_prodlike_namespace_has_history_in_simulated_time_and_a_bait(lab: Settings):
    executor = guarded_connect("executor", settings=lab, max_attempts=1)
    task = f"t{uuid.uuid4().hex[:8]}"
    built = prodlike(executor, task, history=timedelta(days=1))  # 4 ticks of 6 simulated hours, 15 s apart
    bronze = snapshots(executor, table(built, "bronze_orders"))
    assert len(bronze) == 5  # CREATE TABLE's empty snapshot, then one per tick
    gaps = [(b.committed_at - a.committed_at).total_seconds() for a, b in pairwise(bronze[1:])]
    assert all(14 <= gap <= 30 for gap in gaps)  # 15 s real = 6 simulated hours, plus commit time
    planner = guarded_connect("agent_planner", settings=lab)
    for bait in (False, True):
        t = table(built, "gold_daily_revenue", bait=bait)
        assert query(planner, f"SELECT count(*) FROM lab_ro.{t.schema}.{t.table}")[0][0] > 0


def test_the_four_states_hold_the_same_rows_stored_differently(lab: Settings):
    conn = guarded_connect("harness", settings=lab, max_attempts=1)
    query(conn, "CREATE SCHEMA IF NOT EXISTS lab_exp.states_probe")
    suffix, batches = uuid.uuid4().hex[:8], 4
    # Not dropped: purging tables of hundreds of files keeps Polaris's S3 client busy, and commits that meanwhile
    # time out fail ambiguously (docs/research/protocol.md section 2). lab/stack.sh reset removes them.
    built = {
        s: tables.build(conn, s, TableName("lab_exp", "states_probe", f"{s}_{suffix}"), batches=batches)
        for s in tables.STATES
    }
    prints = {s: fingerprint(conn, t) for s, t in built.items()}
    feats = {s: features(conn, t) for s, t in built.items()}
    assert prints["healthy"].rows == 15000
    assert all(p.same_rows(prints["healthy"]) for p in prints.values())
    assert feats["fragmented"].data_files >= batches * feats["healthy"].data_files
    assert feats["skewed"].max_files_per_partition >= batches
    assert feats["skewed"].avg_files_per_partition < feats["fragmented"].avg_files_per_partition
    assert feats["mixed"].position_delete_files >= 1
