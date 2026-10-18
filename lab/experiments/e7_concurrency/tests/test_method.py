"""The method arm: idempotent statements through lakeops.writes, by-key routing, one HTTP attempt per request."""

from datetime import date

import pytest
from e7_concurrency import sql
from e7_concurrency.method import METHOD, METHOD_RESOLVE, by_key, method_writes, resolving_writes
from e7_concurrency.oplog import Outcome
from e7_concurrency.scenario import OpSpec, Scenario, op_streams
from e7_concurrency.tests.fake_trino import FakeConnection
from requests.exceptions import ReadTimeout

from lakeops.writes import insert_row, insert_unless_present, update_once


def scenario(workload: str, **overrides) -> Scenario:
    return Scenario(name="t", workload=workload, writers=4, contention="high", seed=3, ops_per_writer=50, **overrides)


def test_events_are_inserted_if_absent_on_their_id_and_partition():
    conn = FakeConnection()
    result = method_writes(conn, scenario("w1"), METHOD, "w1-2")("w1-2:7", OpSpec(7, 3, 5), date(2026, 1, 2))
    assert result.outcome is Outcome.ACKED
    assert conn.statements == [
        insert_unless_present(
            sql.EVENTS,
            key={"writer_id": "w1-2", "seq": 7, "key": 3, "event_day": date(2026, 1, 2)},
            values={"delta": 5},
        )
    ]


def test_counter_adds_are_applied_once_through_the_ledger():
    conn = FakeConnection()
    method_writes(conn, scenario("w2"), METHOD, "w2-1")("w2-1:9", OpSpec(9, 2, 4), date(2026, 1, 1))
    assert conn.statements == [
        update_once(
            sql.COUNTERS,
            key_column="key",
            key=2,
            value_column="value",
            delta=4,
            ledger_column="applied",
            op_id="w2-1:9",
        )
    ]


def test_an_ambiguous_attempt_is_settled_before_the_write_reports():
    """A timeout, then Trino says the attempt FINISHED: the op is acked after one attempt, nothing re-sent."""

    def respond(statement: str) -> list | BaseException:
        if statement.startswith("SELECT state"):
            return [["FINISHED"]]
        return ReadTimeout("read timed out")

    conn = FakeConnection(respond)
    result = method_writes(conn, scenario("w2"), METHOD, "w2-0")("w2-0:1", OpSpec(1, 0, 1), date(2026, 1, 1))
    assert (result.outcome, result.attempts, result.error) == (Outcome.ACKED, 1, "client-timeout")
    assert conn.cancelled == ["q1"]
    assert sum(not s.startswith("SELECT state") for s in conn.statements) == 1


def test_the_method_needs_the_ledger():
    with pytest.raises(ValueError, match="ledger"):
        method_writes(FakeConnection(), scenario("w2", ledger=False), METHOD, "w2-0")


def test_two_writers_never_share_a_key():
    s = scenario("w2")
    streams = op_streams(s, METHOD.assign)
    keys = [{op.key for op in stream} for stream in streams]
    assert all(a.isdisjoint(b) for i, a in enumerate(keys) for b in keys[i + 1 :])
    assert by_key(0, 6, 4) == 2


def test_the_method_makes_one_http_attempt_per_request():
    assert METHOD.client_max_attempts == 1
    assert METHOD.optimize_scope == "cold-partitions"


def test_the_resolving_method_sends_a_plain_insert_first():
    conn = FakeConnection()
    result = resolving_writes(conn, scenario("w1"), METHOD_RESOLVE, "w1-2")("w1-2:7", OpSpec(7, 3, 5), date(2026, 1, 2))
    assert result.outcome is Outcome.ACKED
    assert conn.statements == [
        insert_row(sql.EVENTS, {"writer_id": "w1-2", "seq": 7, "key": 3, "event_day": date(2026, 1, 2), "delta": 5})
    ]


def test_the_resolving_method_retries_an_insert_whose_snapshot_is_absent_with_the_idempotent_form():
    """A timeout, Trino says FAILED, no snapshot carries the query id: the retry is INSERT … WHERE NOT EXISTS."""
    answers = iter([ReadTimeout("read timed out"), [["FAILED"]], [[0]], [[1]]])

    def respond(_statement: str) -> list | BaseException:
        return next(answers)

    conn = FakeConnection(respond)
    result = resolving_writes(conn, scenario("w1"), METHOD_RESOLVE, "w1-0")("w1-0:1", OpSpec(1, 0, 1), date(2026, 1, 1))
    assert (result.outcome, result.attempts, result.settled) == (Outcome.ACKED, 2, ("FAILED:not-in-snapshots",))
    assert "$snapshots" in conn.statements[2]
    assert "WHERE NOT EXISTS" in conn.statements[3]
