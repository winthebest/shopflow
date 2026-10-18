"""The retry loop: never two attempts at once, an ambiguous attempt settled before the next, backoff with jitter."""

import pytest
import requests
from trino.exceptions import TrinoQueryError

from lakeops.writes import RetryPolicy, write

TIMEOUT = requests.exceptions.ReadTimeout("read timed out")
SYNTAX = TrinoQueryError({"errorName": "SYNTAX_ERROR", "errorType": "USER_ERROR", "message": "bad"}, "q")
COMMIT = TrinoQueryError(
    {
        "errorName": "ICEBERG_COMMIT_ERROR",
        "errorType": "EXTERNAL",
        "message": "conflict",
        "failureInfo": {"type": "org.apache.iceberg.exceptions.CommitFailedException"},
    },
    "q",
)
COMMIT_CLASS = "ICEBERG_COMMIT_ERROR/org.apache.iceberg.exceptions.CommitFailedException"


class FakeConnection:
    """Each attempt of the write takes the next (query id, error) from `attempts`; each state poll takes the next
    state from `states` (None: no row yet; an exception: the poll fails)."""

    def __init__(self, attempts: list, states: list | None = None, snapshots: list | None = None) -> None:
        self.attempts, self.states, self.snapshots = list(attempts), list(states or []), list(snapshots or [])
        self.sent: list[str] = []
        self.cancelled: list[str | None] = []
        self.polls = 0

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn, self.query_id, self._rows = conn, None, []

    def execute(self, statement: str) -> None:
        if statement.startswith("SELECT state FROM system.runtime.queries"):
            self.conn.polls += 1
            state = self.conn.states.pop(0) if self.conn.states else None
            if isinstance(state, BaseException):
                raise state
            self._rows = [[state]] if state else []
            return
        if "$snapshots" in statement:
            found = self.conn.snapshots.pop(0)  # True / False: whether the attempt's snapshot is there
            if isinstance(found, BaseException):
                raise found
            self._rows = [[int(found)]]
            return
        self.conn.sent.append(statement)
        self.query_id, error = self.conn.attempts.pop(0)
        if error is not None:
            raise error
        self._rows = [[1]]

    def fetchall(self) -> list:
        return self._rows

    def cancel(self) -> None:
        self.conn.cancelled.append(self.query_id)


class Clock:
    def __init__(self) -> None:
        self.now, self.slept = 0.0, []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.now += seconds


POLICY = RetryPolicy(attempts=4, base_seconds=0.1, cap_seconds=3.0, settle_seconds=2.0, poll_seconds=0.5)


def run(conn: FakeConnection, policy: RetryPolicy = POLICY, clock: Clock | None = None, **resolve):
    clock = clock or Clock()
    return write(conn, "MERGE ...", policy, sleep=clock.sleep, jitter=lambda: 1.0, clock=clock, **resolve), clock


def test_a_write_that_succeeds_is_sent_once():
    result, _ = run(FakeConnection([("q1", None)]))
    assert (result.state, result.attempts, result.query_ids) == ("committed", 1, ("q1",))


def test_a_user_error_is_not_retried_or_settled():
    conn = FakeConnection([("q1", SYNTAX)])
    result, _ = run(conn)
    assert (result.state, result.attempts, result.error) == ("failed", 1, "SYNTAX_ERROR")
    assert (conn.cancelled, conn.polls) == ([], 0)


def test_an_ambiguous_attempt_that_finished_is_not_sent_again():
    conn = FakeConnection([("q1", TIMEOUT)], states=["RUNNING", "FINISHED"])
    result, _ = run(conn)
    assert (result.state, result.attempts, result.settled) == ("committed", 1, ("FINISHED",))
    assert len(conn.sent) == 1
    assert conn.cancelled == ["q1"]


def test_an_ambiguous_attempt_that_failed_is_retried_after_it_ended():
    conn = FakeConnection([("q1", TIMEOUT), ("q2", None)], states=["RUNNING", "FAILED"])
    result, clock = run(conn)
    assert (result.state, result.attempts, result.query_ids, result.settled) == (
        "committed",
        2,
        ("q1", "q2"),
        ("FAILED",),
    )
    assert clock.slept == [0.5, 0.1]  # one poll interval while q1 ran, then the backoff before attempt 2


def test_an_attempt_that_never_settles_ends_the_write_unknown_without_a_second_attempt():
    conn = FakeConnection([("q1", TIMEOUT), ("q2", None)], states=[])
    result, _ = run(conn)
    assert (result.state, result.attempts, result.settled) == ("unknown", 1, ("unsettled:not-found",))
    assert len(conn.sent) == 1
    assert conn.polls == 5  # at 0, 0.5, 1.0, 1.5 and 2.0 s, the settle deadline


def test_a_failed_poll_is_polled_again():
    conn = FakeConnection([("q1", TIMEOUT)], states=[requests.exceptions.ConnectionError(), "FINISHED"])
    result, _ = run(conn)
    assert result.state == "committed"


@pytest.mark.parametrize(
    ("retry_without_query_id", "state", "attempts"), [(False, "unknown", 1), (True, "committed", 2)]
)
def test_an_attempt_without_a_query_id_is_retried_only_when_allowed(
    retry_without_query_id: bool, state: str, attempts: int
):
    conn = FakeConnection([(None, requests.exceptions.ConnectionError()), ("q2", None)])
    policy = RetryPolicy(attempts=4, retry_without_query_id=retry_without_query_id)
    result, _ = run(conn, policy)
    assert (result.state, result.attempts, result.settled) == (state, attempts, ("no-query-id",))


def test_ambiguous_failures_to_the_last_attempt_end_unknown():
    conn = FakeConnection([(f"q{i}", COMMIT) for i in range(1, 5)], states=["FAILED"] * 4)
    result, clock = run(conn)
    assert (result.state, result.attempts, result.error) == ("unknown", 4, COMMIT_CLASS)
    assert [s for s in clock.slept if s != 0.5] == [0.1, 0.2, 0.4]  # backoff doubles; none after the last attempt


def test_definite_failures_are_not_settled_and_end_failed():
    conn = FakeConnection([(f"q{i}", COMMIT) for i in range(1, 5)])
    policy = RetryPolicy(attempts=4, base_seconds=1.0, cap_seconds=2.5, definite=frozenset({COMMIT_CLASS}))
    result, clock = run(conn, policy)
    assert (result.state, result.attempts, result.settled) == ("failed", 4, ())
    assert (conn.polls, conn.cancelled) == (0, [])
    assert clock.slept == [1.0, 2.0, 2.5]  # capped


TABLE = "lab_exp.lab_e7.events"


def test_a_failed_attempt_whose_snapshot_is_there_committed():
    """A cancel can fail a query whose commit already landed: the table's snapshots say so."""
    conn = FakeConnection([("q1", TIMEOUT), ("q2", None)], states=["FAILED"], snapshots=[True])
    result, _ = run(conn, table=TABLE)
    assert (result.state, result.attempts, result.settled) == ("committed", 1, ("FAILED:in-snapshots",))
    assert len(conn.sent) == 1


def test_a_query_gone_from_the_history_is_resolved_from_the_snapshots():
    conn = FakeConnection([("q1", TIMEOUT), ("q2", None)], states=[], snapshots=[False])
    result, _ = run(conn, table=TABLE, retry_statement="INSERT ... WHERE NOT EXISTS ...")
    assert (result.state, result.attempts) == ("committed", 2)
    assert result.settled == ("unsettled:not-found:not-in-snapshots",)
    assert conn.sent == ["MERGE ...", "INSERT ... WHERE NOT EXISTS ..."]  # retries use the idempotent form


def test_a_query_still_running_is_not_resolved_from_the_snapshots():
    conn = FakeConnection([("q1", TIMEOUT)], states=["RUNNING"] * 10, snapshots=[])
    result, _ = run(conn, table=TABLE)
    assert (result.state, result.settled) == ("unknown", ("unsettled:RUNNING",))


def test_attempts_resolved_absent_end_failed_not_unknown():
    conn = FakeConnection([(f"q{i}", COMMIT) for i in range(1, 5)], states=["FAILED"] * 4, snapshots=[False] * 4)
    result, _ = run(conn, table=TABLE)
    assert (result.state, result.attempts) == ("failed", 4)


def test_unreadable_snapshots_fall_back_to_the_query_state():
    conn = FakeConnection(
        [("q1", TIMEOUT), ("q2", None)], states=["FAILED"], snapshots=[requests.exceptions.ConnectionError()]
    )
    result, _ = run(conn, table=TABLE)
    assert (result.state, result.attempts, result.settled) == ("committed", 2, ("FAILED",))
