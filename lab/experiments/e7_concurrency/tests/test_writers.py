"""The writer loop logs every op before sending it; the maintenance loop keeps its own log; one statement per op."""

import json
from datetime import date
from pathlib import Path

from e7_concurrency import sql
from e7_concurrency.oplog import OpLogWriter, Outcome, read_oplog
from e7_concurrency.scenario import BASELINE, Arm, OpSpec, Scenario
from e7_concurrency.tests.fake_trino import FakeConnection
from e7_concurrency.writers import (
    Attempted,
    baseline_writes,
    execute_once,
    maintenance_statement,
    run_maintenance,
    run_ops,
)
from requests.exceptions import ReadTimeout


class Clock:
    """Advances by `step` on every reading, and by the slept time on `sleep`."""

    def __init__(self, step: float = 1.0) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_each_op_is_logged_before_it_is_sent_and_completed_after(tmp_path: Path):
    path = tmp_path / "w1-0.jsonl"
    on_disk_when_sent = []

    def apply(oid: str, op: OpSpec, day: date) -> Attempted:
        on_disk_when_sent.append(path.read_text().count('"invoke"'))
        return Attempted(Outcome.ACKED if op.seq != 2 else Outcome.UNKNOWN, 1, query_ids=(f"q{op.seq}",))

    ops = [OpSpec(seq, seq, 1) for seq in range(1, 6)]
    with OpLogWriter(path, "w1-0") as log:
        sent = run_ops(
            ops,
            table="events",
            log=log,
            apply=apply,
            event_day=lambda _elapsed: date(2026, 1, 1),
            start=0.0,
            deadline=6.0,  # the clock reads 1, 3, 5 when deciding to send, then 7: three ops go out
            clock=Clock(),
        )
    assert sent == 3
    assert on_disk_when_sent == [1, 2, 3]
    logged = read_oplog([path])
    assert [(o.seq, o.outcome, o.query_ids) for o in logged] == [
        (1, Outcome.ACKED, ("q1",)),
        (2, Outcome.UNKNOWN, ("q2",)),
        (3, Outcome.ACKED, ("q3",)),
    ]
    assert all(o.latency_ms == 1000.0 for o in logged)


def test_the_event_day_follows_the_time_since_the_start(tmp_path: Path):
    days = []
    with OpLogWriter(tmp_path / "w1-0.jsonl", "w1-0") as log:
        run_ops(
            [OpSpec(1, 0, 1), OpSpec(2, 0, 1)],
            table="events",
            log=log,
            apply=lambda _oid, _op, day: days.append(day) or Attempted(Outcome.ACKED, 1),
            event_day=lambda elapsed: date(2026, 1, 1 + int(elapsed)),
            start=0.0,
            deadline=100.0,
            clock=Clock(),
        )
    assert days == [date(2026, 1, 2), date(2026, 1, 4)]  # sent 1 s and 3 s after the start


def test_execute_once_waits_for_the_result_and_keeps_the_query_id():
    conn = FakeConnection()
    assert execute_once(conn, "INSERT 1", frozenset()) == Attempted(Outcome.ACKED, 1, query_ids=("q1",))
    conn = FakeConnection(lambda _s: ReadTimeout("read timed out"))
    result = execute_once(conn, "INSERT 1", frozenset())
    assert (result.outcome, result.error, result.query_ids) == (Outcome.UNKNOWN, "client-timeout", ("q1",))
    assert execute_once(conn, "INSERT 1", frozenset({"client-timeout"})).outcome is Outcome.FAILED


def scenario(workload: str, **overrides) -> Scenario:
    return Scenario(name="t", workload=workload, writers=2, contention="medium", seed=1, **overrides)


def test_baseline_writes_send_one_statement_per_op():
    conn = FakeConnection()
    insert = baseline_writes(conn, scenario("w1"), BASELINE, "w1-0")
    insert("w1-0:4", OpSpec(4, 5, 3), date(2026, 1, 1))
    add = baseline_writes(conn, scenario("w2"), BASELINE, "w2-1")
    add("w2-1:9", OpSpec(9, 5, 3), date(2026, 1, 1))
    assert conn.statements == [
        sql.insert_event("w1-0", 4, 5, 3, date(2026, 1, 1)),
        sql.add_to_counter("w2-1:9", 5, 3),
    ]


def test_maintenance_runs_on_its_schedule_and_logs_each_run(tmp_path: Path):
    clock = Clock(step=0.0)
    conn = FakeConnection(lambda s: ReadTimeout("slow") if "fail" in s else [[1]])
    statements = iter(["OPTIMIZE a", "OPTIMIZE fail", "OPTIMIZE b"])
    runs = run_maintenance(
        conn,
        statement=lambda _elapsed: next(statements),
        log_path=tmp_path / "maintenance.jsonl",
        every=5.0,
        definite=frozenset(),
        start=0.0,
        deadline=12.0,
        clock=clock,
        sleep=clock.sleep,
    )
    records = [json.loads(line) for line in (tmp_path / "maintenance.jsonl").read_text().splitlines()]
    assert runs == 3
    assert [(r["elapsed_s"], r["sql"], r["outcome"], r["error"]) for r in records] == [
        (0.0, "OPTIMIZE a", "acked", None),
        (5.0, "OPTIMIZE fail", "unknown", "client-timeout"),
        (10.0, "OPTIMIZE b", "acked", None),
    ]


def test_maintenance_compacts_the_whole_table_or_only_past_days():
    events = scenario("w1", maintenance="optimize", day_seconds=10.0)
    assert maintenance_statement(events, BASELINE)(25.0) == sql.optimize(sql.EVENTS)
    cold = Arm("cold", optimize_scope="cold-partitions")
    assert maintenance_statement(events, cold)(25.0) == sql.optimize(sql.EVENTS, before_day=date(2026, 1, 3))
    counters = scenario("w2", maintenance="optimize")
    assert maintenance_statement(counters, cold)(25.0) == sql.optimize(sql.COUNTERS)
