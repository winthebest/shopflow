"""The runner: fresh tables, writer processes on one barrier (killed when injected or still running after the drain),
and the results row graded from the op logs and the final tables."""

import json
from pathlib import Path

from e7_concurrency import sql
from e7_concurrency.oplog import OpLogWriter, Outcome, read_oplog
from e7_concurrency.runner import Kill, Launched, collect, launch, prepare
from e7_concurrency.scenario import BASELINE, Scenario
from e7_concurrency.tests.fake_trino import FakeConnection, hanging_connector, quick_connector, slow_connector


def scenario(**overrides) -> Scenario:
    values = {"name": "t", "workload": "w2", "writers": 2, "contention": "high", "seed": 1, "ops_per_writer": 1000}
    return Scenario(**(values | overrides))


def test_prepare_recreates_checks_and_seeds():
    conn, checked = FakeConnection(), []
    prepare(conn, scenario(), lambda _conn, table: checked.append(table))
    assert conn.statements == [*sql.recreate_tables(), sql.seed_counters(range(4))]
    assert checked == [sql.EVENTS, sql.COUNTERS]
    conn = FakeConnection()
    prepare(conn, scenario(workload="w1"), lambda _conn, _table: None)
    assert conn.statements == sql.recreate_tables()


def write_log(run_dir: Path, writer: str, ops: list[tuple[int, int, int, Outcome, str | None]]) -> None:
    with OpLogWriter(run_dir / f"{writer}.jsonl", writer) as log:
        for seq, key, delta, outcome, error in ops:
            oid = log.invoke(seq, "counters", key, delta)
            log.complete(oid, outcome, attempts=1, error=error, latency_ms=10.0 * seq, query_ids=[f"{writer}-q{seq}"])


def answering(counters: list, snapshots: list):
    def respond(statement: str) -> list:
        if statement == sql.READ_EVENTS:
            return []
        if statement == sql.READ_COUNTERS:
            return counters
        if statement.startswith("SELECT snapshot_id"):
            return snapshots
        raise AssertionError(f"unexpected statement {statement}")

    return respond


def test_collect_grades_the_run_and_builds_its_row(tmp_path: Path):
    write_log(tmp_path, "w2-0", [(1, 0, 3, Outcome.ACKED, None), (2, 1, 4, Outcome.UNKNOWN, "client-timeout")])
    write_log(tmp_path, "w2-1", [(1, 0, 5, Outcome.ACKED, None)])
    counters = [[0, 8, ["w2-0:1", "w2-1:1"]], [1, 4, ["w2-0:2"]], [2, 0, []], [3, 0, []]]
    snapshots = [
        [1, "append", {"added-files-size": "100", "total-files-size": "100"}],
        [2, "overwrite", {"added-files-size": "300", "total-files-size": "200", "trino_query_id": "w2-0-q1"}],
    ]
    row = collect(FakeConnection(answering(counters, snapshots)), scenario(), BASELINE, tmp_path, Launched(2.0))
    assert row["valid"] is True
    assert (row["lost_update"], row["duplicates"], row["wrong_final_rows"], row["unknown_present"]) == (0, 0, 0, 1)
    assert row["cct"] == 1.0  # 2 acked commits in 2 s; the unknown op that committed does not count
    assert row["max_ledger"] == 2
    assert row["write_amplification"] == 2.0  # 400 bytes written for 200 kept
    assert row["acked_with_snapshot_query_id"] == 0.5
    assert json.loads(row["error_classes"]) == {"client-timeout": 1}
    assert row["latency_p95_ms"] is not None


def test_collect_marks_a_lost_add_invalid(tmp_path: Path):
    write_log(tmp_path, "w2-0", [(1, 0, 3, Outcome.ACKED, None)])
    counters = [[0, 0, []], [1, 0, []], [2, 0, []], [3, 0, []]]
    row = collect(FakeConnection(answering(counters, [])), scenario(), BASELINE, tmp_path, Launched(2.0))
    assert (row["valid"], row["cct"], row["lost_update"]) == (False, None, 1)


def test_collect_without_the_ledger_grades_by_bounds(tmp_path: Path):
    write_log(tmp_path, "w2-0", [(1, 0, 3, Outcome.ACKED, None), (2, 0, 4, Outcome.UNKNOWN, None)])

    def respond(statement: str) -> list:
        return {sql.READ_EVENTS: [], sql.READ_COUNTERS_NO_LEDGER: [[0, 7], [1, 0], [2, 0], [3, 0]]}.get(statement, [])

    row = collect(FakeConnection(respond), scenario(ledger=False), BASELINE, tmp_path, Launched(1.0))
    assert (row["valid"], row["bounds_below"], row["bounds_above"], row["max_ledger"]) == (True, 0, 0, None)
    assert row["cct"] == 1.0


def test_launch_runs_the_writers_for_the_write_phase(tmp_path: Path):
    s = scenario(seconds=1.0, drain_seconds=5.0)
    launched = launch(s, BASELINE, tmp_path, quick_connector)
    assert (launched.killed, launched.stragglers, launched.crashed) == ((), (), ())
    ops = read_oplog(sorted(tmp_path.glob("w2-*.jsonl")))
    assert {o.writer for o in ops} == {"w2-0", "w2-1"}
    assert all(o.outcome is Outcome.ACKED for o in ops)
    assert 1.0 <= launched.seconds < 5.0


def test_launch_kills_writers_still_running_after_the_drain(tmp_path: Path):
    s = scenario(seconds=0.5, drain_seconds=0.5)
    launched = launch(s, BASELINE, tmp_path, hanging_connector)
    assert set(launched.stragglers) == {"w2-0", "w2-1"}
    ops = read_oplog(sorted(tmp_path.glob("w2-*.jsonl")))
    assert [(o.id, o.outcome, o.error) for o in ops] == [
        ("w2-0:1", Outcome.UNKNOWN, "no-completion"),
        ("w2-1:1", Outcome.UNKNOWN, "no-completion"),
    ]


def test_launch_kill_injection_leaves_at_most_the_open_op_unknown(tmp_path: Path):
    s = scenario(seconds=2.0, drain_seconds=5.0)
    launched = launch(s, BASELINE, tmp_path, slow_connector, kill=Kill(writer=0, at=0.5))
    assert launched.killed == ("w2-0",)
    ops = read_oplog(sorted(tmp_path.glob("w2-*.jsonl")))
    killed = [o for o in ops if o.writer == "w2-0"]
    survivor = [o for o in ops if o.writer == "w2-1"]
    assert sum(o.outcome is Outcome.UNKNOWN for o in killed) <= 1
    assert len(killed) < len(survivor)  # 0.5 s of ops against 2 s
