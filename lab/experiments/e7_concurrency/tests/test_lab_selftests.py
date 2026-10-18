"""The oracle's three self-tests on real tables in the lab (`make lab-test`), plus the tables as designed.

Each test runs a short baseline scenario with the runner, then changes the final tables the way the fault would, and
checks that the oracle reports exactly that fault.
"""

import time
from pathlib import Path

import pytest
from e7_concurrency import sql
from e7_concurrency.oplog import OpLogWriter, Outcome, read_oplog
from e7_concurrency.runner import (
    ADMIN,
    Kill,
    Launched,
    collect,
    lab_check_table,
    lab_connector,
    launch,
    prepare,
    query,
)
from e7_concurrency.scenario import BASELINE, Scenario
from e7_concurrency.writers import execute_once

pytestmark = pytest.mark.lab


def short(workload: str, **overrides) -> Scenario:
    values = {"name": f"selftest-{workload}", "workload": workload, "writers": 2, "contention": "low", "seed": 11}
    return Scenario(**(values | {"seconds": 5.0, "drain_seconds": 30.0} | overrides))


def run_short(run_dir: Path, scenario: Scenario, kill: Kill | None = None):
    admin = lab_connector(ADMIN)
    prepare(admin, scenario, lab_check_table, retire=f"selftest_{time.time_ns()}")
    launched = launch(scenario, BASELINE, run_dir, lab_connector, kill)
    return admin, launched, collect(admin, scenario, BASELINE, run_dir, launched)


def first_acked(run_dir: Path, scenario: Scenario):
    ops = read_oplog(sorted(run_dir.glob(f"{scenario.workload}-*.jsonl")))
    return next(op for op in ops if op.outcome is Outcome.ACKED)


def test_tables_are_created_as_designed(lab, tmp_path: Path):
    admin = lab_connector(ADMIN)
    prepare(admin, short("w2"), lab_check_table, retire=f"selftest_{time.time_ns()}")
    events = query(admin, f"SHOW CREATE TABLE {sql.EVENTS}")[0][0]
    counters = query(admin, f"SHOW CREATE TABLE {sql.COUNTERS}")[0][0]
    assert "format_version = 2" in events
    assert "bucket(key, 8)" in events
    assert "event_day" in events
    assert "format_version = 2" in counters
    assert query(admin, f"SELECT count(*), sum(value), max(cardinality(applied)) FROM {sql.COUNTERS}") == [[32, 0, 0]]


def test_self_test_dropped_acked_event_is_a_lost_update(lab, tmp_path: Path):
    scenario = short("w1")
    admin, launched, row = run_short(tmp_path, scenario)
    assert (row["valid"], row["lost_update"], row["duplicates"]) == (True, 0, 0)
    victim = first_acked(tmp_path, scenario)
    query(admin, f"DELETE FROM {sql.EVENTS} WHERE writer_id = '{victim.writer}' AND seq = {victim.seq}")
    row = collect(admin, scenario, BASELINE, tmp_path, launched)
    assert (row["valid"], row["lost_update"], row["wrong_final_rows"]) == (False, 1, 0)


def test_self_test_dropped_ledger_entry_is_a_lost_update(lab, tmp_path: Path):
    scenario = short("w2")
    admin, launched, row = run_short(tmp_path, scenario)
    assert (row["valid"], row["lost_update"], row["duplicates"]) == (True, 0, 0)
    victim = first_acked(tmp_path, scenario)
    query(
        admin,
        f"UPDATE {sql.COUNTERS} SET value = value - {victim.delta}, "
        f"applied = filter(applied, x -> x <> '{victim.id}') WHERE key = {victim.key}",
    )
    row = collect(admin, scenario, BASELINE, tmp_path, launched)
    assert (row["valid"], row["lost_update"], row["wrong_final_rows"]) == (False, 1, 0)


def test_self_test_killed_client_leaves_one_unknown_op_and_a_blind_retry_duplicates(lab, tmp_path: Path):
    scenario = short("w1", seconds=6.0)
    admin, launched, row = run_short(tmp_path, scenario, kill=Kill(writer=0, at=3.0))
    assert launched.killed == ("w1-0",)
    assert (row["valid"], row["lost_update"]) == (True, 0)
    killed = [op for op in read_oplog([tmp_path / "w1-0.jsonl"]) if op.outcome is Outcome.UNKNOWN]
    assert len(killed) <= 1  # the op in flight when the writer died, if it was mid-request

    # A blind retry of an op that had committed: the oracle must count it as a duplicate.
    victim = first_acked(tmp_path, scenario)
    execute_once(
        admin,
        sql.insert_event(victim.writer, victim.seq, victim.key, victim.delta, scenario.event_day(0, 0)),
        frozenset(),
    )
    row = collect(admin, scenario, BASELINE, tmp_path, launched)
    assert (row["duplicates"], row["lost_update"]) == (1, 0)


def test_self_test_stale_retry_loses_the_other_writers_add(lab, tmp_path: Path):
    """A reads key 0, writes 0 + 3 (the client loses the answer), B adds 4, A retries its stale absolute write."""
    scenario = short("w2", contention="high")
    admin = lab_connector(ADMIN)
    prepare(admin, scenario, lab_check_table, retire=f"selftest_{time.time_ns()}")
    stale = f"UPDATE {sql.COUNTERS} SET value = 3, applied = ARRAY['w2-0:1'] WHERE key = 0"
    with OpLogWriter(tmp_path / "w2-0.jsonl", "w2-0") as a, OpLogWriter(tmp_path / "w2-1.jsonl", "w2-1") as b:
        assert query(admin, f"SELECT value, applied FROM {sql.COUNTERS} WHERE key = 0") == [[0, []]]
        oid_a = a.invoke(1, "counters", 0, 3)
        query(admin, stale)
        oid_b = b.invoke(1, "counters", 0, 4)
        result = execute_once(admin, sql.add_to_counter(oid_b, 0, 4), frozenset())
        b.complete(oid_b, result.outcome, attempts=1, query_ids=result.query_ids)
        query(admin, stale)
        a.complete(oid_a, Outcome.ACKED, attempts=2)
    assert result.outcome is Outcome.ACKED
    row = collect(admin, scenario, BASELINE, tmp_path, Launched(1.0))
    assert (row["valid"], row["lost_update"], row["wrong_final_rows"], row["duplicates"]) == (False, 1, 0, 0)
