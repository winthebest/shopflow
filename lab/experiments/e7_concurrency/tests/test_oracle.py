"""The oracle on synthetic final states, including its three self-tests: an acked op dropped, a client killed right
after its commit, and a stale retry after another writer's add. The lab runs repeat the three on real tables."""

import dataclasses
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterable
from pathlib import Path

import pytest
from e7_concurrency.oplog import Op, OpLogWriter, Outcome, read_oplog
from e7_concurrency.oracle import CounterRow, EventRow, Verdict, correct_commit_throughput, grade, grade_bounds

ACKED, FAILED, UNKNOWN = Outcome.ACKED, Outcome.FAILED, Outcome.UNKNOWN
KEYS = range(4)


def op(writer: str, seq: int, table: str, key: int, delta: int, outcome: Outcome, group: str | None = None) -> Op:
    return Op(writer=writer, seq=seq, table=table, key=key, delta=delta, outcome=outcome, group=group, attempts=1)


def state(ops: Iterable[Op], present: Iterable[str]) -> tuple[list[EventRow], list[CounterRow]]:
    """The correct final tables when exactly the ops in `present` committed, each once."""
    present = set(present)
    events = [EventRow(o.writer, o.seq, o.key, o.delta) for o in ops if o.table == "events" and o.id in present]
    counters = []
    for key in KEYS:
        applied = [o for o in ops if o.table == "counters" and o.key == key and o.id in present]
        counters.append(CounterRow(key, sum(o.delta for o in applied), tuple(o.id for o in applied)))
    return events, counters


def kinds(verdict: Verdict) -> list[str]:
    return sorted(f.kind for f in verdict.findings)


# Two event writers and two counter writers; one definite failure and one unknown op on each table.
OPS = [
    op("w1-0", 1, "events", 0, 5, ACKED),
    op("w1-0", 2, "events", 1, 7, FAILED),
    op("w1-1", 1, "events", 2, 2, ACKED),
    op("w1-1", 2, "events", 3, 4, UNKNOWN),
    op("w2-0", 1, "counters", 1, 3, ACKED),
    op("w2-0", 2, "counters", 1, 4, ACKED),
    op("w2-1", 1, "counters", 1, 6, UNKNOWN),
    op("w2-1", 2, "counters", 2, 1, FAILED),
]
COMMITTED = {o.id for o in OPS if o.outcome is ACKED} | {"w2-1:1"}  # one unknown op committed, the other did not


def test_correct_state_is_valid_and_resolves_unknown_ops():
    verdict = grade(OPS, *state(OPS, COMMITTED), seeded_keys=KEYS)
    assert verdict.valid
    assert verdict.findings == ()
    assert (verdict.lost_update, verdict.duplicates, verdict.wrong_final_rows) == (0, 0, 0)
    assert (verdict.acked, verdict.failed, verdict.unknown) == (4, 2, 2)
    assert (verdict.unknown_present, verdict.unknown_absent) == (1, 1)


# Self-test 1: an acked op dropped from the final state.


def test_self_test_dropped_acked_event_is_a_lost_update():
    events, counters = state(OPS, COMMITTED)
    events = [r for r in events if (r.writer, r.seq) != ("w1-1", 1)]
    verdict = grade(OPS, events, counters, KEYS)
    assert (verdict.lost_update, verdict.wrong_final_rows) == (1, 0)
    assert not verdict.valid
    assert [f.subject for f in verdict.findings] == ["w1-1:1"]


def test_self_test_dropped_acked_add_is_a_lost_update():
    """The add is gone from the ledger and from the value: the row is consistent, the op is lost."""
    events, counters = state(OPS, COMMITTED - {"w2-0:1"})
    verdict = grade(OPS, events, counters, KEYS)
    assert (verdict.lost_update, verdict.wrong_final_rows) == (1, 0)
    assert not verdict.valid


def test_self_test_dropped_ledger_entry_with_value_kept_is_lost_and_wrong():
    events, counters = state(OPS, COMMITTED)
    counters[1] = dataclasses.replace(counters[1], applied=("w2-0:2", "w2-1:1"))  # value still includes w2-0:1
    verdict = grade(OPS, events, counters, KEYS)
    assert (verdict.lost_update, verdict.wrong_final_rows) == (1, 1)
    assert kinds(verdict) == ["ledger-mismatch", "lost"]


# Self-test 2: the client is killed right after its commit, so the log has an invoke and no complete.

WRITER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from e7_concurrency.oplog import OpLogWriter
    log = OpLogWriter(Path(sys.argv[1]), "w1-0")
    log.invoke(1, "events", 3, 9)
    print("sent", flush=True)  # stands for "the request left and the server committed it"
    time.sleep(60)
    """
)


def test_self_test_client_killed_after_commit_leaves_an_unknown_op(tmp_path: Path):
    path = tmp_path / "w1-0.jsonl"
    writer = subprocess.Popen(  # noqa: S603  # this interpreter, a constant script
        [sys.executable, "-c", WRITER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert writer.stdout is not None
    assert writer.stdout.readline() == "sent\n"
    writer.kill()  # SIGKILL: no handler, no flush at exit
    writer.wait(timeout=10)

    [killed] = read_oplog([path])
    assert (killed.id, killed.outcome, killed.error) == ("w1-0:1", UNKNOWN, "no-completion")

    committed = [EventRow("w1-0", 1, 3, 9)]
    verdict = grade([killed], committed, [], seeded_keys=[])
    assert verdict.valid
    assert (verdict.unknown_present, verdict.duplicates) == (1, 0)

    verdict = grade([killed], [], [], seeded_keys=[])
    assert verdict.valid
    assert verdict.unknown_absent == 1


def test_self_test_blind_retry_of_a_committed_unknown_op_is_a_duplicate():
    """The writer did not know the first attempt had committed, retried, and the retry committed too."""
    events, counters = state(OPS, COMMITTED | {"w1-1:2"})
    events.append(EventRow("w1-1", 2, 3, 4))
    counters[1] = CounterRow(1, counters[1].value + 6, (*counters[1].applied, "w2-1:1"))
    verdict = grade(OPS, events, counters, KEYS)
    assert verdict.duplicates == 2
    assert verdict.valid  # a duplicate does not void the throughput, it is reported on its own
    assert kinds(verdict) == ["duplicate", "duplicate"]


# Self-test 3: a stale retry after another writer added to the same key.
# A's first attempt timed out but committed (key 1: 0 -> 3). B added 4 (-> 7) and was acked. A's application-level
# retry still holds its first read (value 0, empty ledger) and writes 0 + 3 over B's add; the client sees A acked.

STALE = [op("w2-0", 1, "counters", 1, 3, ACKED), op("w2-1", 1, "counters", 1, 4, ACKED)]


def seeded_rows(key_row: CounterRow) -> list[CounterRow]:
    return [key_row if key == key_row.key else CounterRow(key, 0, ()) for key in KEYS]


def test_self_test_stale_retry_overwriting_a_newer_add_is_a_lost_update():
    verdict = grade(STALE, [], seeded_rows(CounterRow(1, 3, ("w2-0:1",))), KEYS)
    assert (verdict.lost_update, verdict.wrong_final_rows, verdict.duplicates) == (1, 0, 0)
    assert [f.subject for f in verdict.findings] == ["w2-1:1"]


def test_self_test_stale_value_with_appended_ledger_is_wrong_and_duplicate():
    """Same retry, but the ledger is appended in SQL while the value comes from the stale read."""
    verdict = grade(STALE, [], seeded_rows(CounterRow(1, 3, ("w2-0:1", "w2-1:1", "w2-0:1"))), KEYS)
    assert (verdict.lost_update, verdict.wrong_final_rows, verdict.duplicates) == (0, 1, 1)
    assert kinds(verdict) == ["duplicate", "ledger-mismatch"]


# Other faults.


def test_failed_op_present_is_a_wrong_row():
    events, counters = state(OPS, COMMITTED | {"w1-0:2"})
    verdict = grade(OPS, events, counters, KEYS)
    assert (verdict.wrong_final_rows, verdict.failed_present, verdict.lost_update) == (1, 1, 0)
    assert not verdict.valid


def test_unlogged_and_altered_rows_are_wrong():
    events, counters = state(OPS, COMMITTED)
    events.append(EventRow("w9", 1, 0, 1))
    events[0] = dataclasses.replace(events[0], delta=50)
    counters[2] = CounterRow(2, 8, ("w9:2",))
    verdict = grade(OPS, events, counters, KEYS)
    assert verdict.wrong_final_rows == 3
    assert kinds(verdict) == ["altered", "ledger-mismatch", "unlogged", "unlogged"]


def test_split_key_from_concurrent_merges_is_an_extra_row_and_a_duplicate():
    """Two MERGEs read the same row (ledger [x]) and each wrote its own successor: key 1 now has two rows."""
    ops = [op("w2-0", 1, "counters", 1, 2, ACKED), op("w2-0", 2, "counters", 1, 3, ACKED)]
    ops.append(op("w2-1", 1, "counters", 1, 5, ACKED))
    rows = [CounterRow(1, 5, ("w2-0:1", "w2-0:2")), CounterRow(1, 7, ("w2-0:1", "w2-1:1"))]
    rows += [CounterRow(key, 0, ()) for key in KEYS if key != 1]
    verdict = grade(ops, [], rows, KEYS)
    assert (verdict.lost_update, verdict.wrong_final_rows, verdict.duplicates) == (0, 1, 1)
    assert kinds(verdict) == ["duplicate", "extra-row"]


def test_missing_and_unseeded_keys_are_wrong():
    ops = [op("w2-0", 1, "counters", 0, 1, ACKED)]
    rows = [CounterRow(key, 0, ()) for key in (1, 2, 3, 9)]
    verdict = grade(ops, [], rows, KEYS)
    assert (verdict.lost_update, verdict.wrong_final_rows) == (1, 2)
    assert kinds(verdict) == ["lost", "missing-row", "unseeded-key"]


def test_torn_group_is_wrong_and_its_missing_ops_lost():
    ops = [op("sink", seq, "events", seq, 1, ACKED, group="sink:commit-1") for seq in range(3)]
    ops += [op("sink", seq, "events", seq, 1, UNKNOWN, group="sink:commit-2") for seq in range(3, 5)]
    events = [EventRow("sink", seq, seq, 1) for seq in (0, 1, 3)]
    verdict = grade(ops, events, [], seeded_keys=[])
    assert verdict.torn_groups == 2
    assert (verdict.lost_update, verdict.wrong_final_rows) == (1, 3)


def test_op_ids_must_be_unique():
    with pytest.raises(ValueError, match="twice"):
        grade([OPS[0], OPS[0]], [], [], KEYS)


def test_correct_commit_throughput_counts_acked_commits_of_valid_runs_only():
    ops = [op("sink", seq, "events", seq, 1, ACKED, group="sink:commit-1") for seq in range(3)]
    ops += [op("w1-0", 1, "events", 0, 1, ACKED), op("w1-0", 2, "events", 0, 1, UNKNOWN)]
    present = {o.id for o in ops}  # the unknown op committed: it is still not counted
    verdict = grade(ops, state(ops, present)[0], [], seeded_keys=[])
    assert correct_commit_throughput(verdict, ops, seconds=4.0) == 0.5  # 2 acked commits in 4 s

    verdict = grade(ops, [], [], seeded_keys=[])
    assert correct_commit_throughput(verdict, ops, seconds=4.0) is None


def test_log_written_by_writers_grades_like_the_ops(tmp_path: Path):
    with OpLogWriter(tmp_path / "w1-0.jsonl", "w1-0") as log:
        oid = log.invoke(1, "events", 0, 5)
        log.complete(oid, ACKED, attempts=1, latency_ms=12.5, query_ids=["q1"], snapshot_id=42)
        oid = log.invoke(2, "events", 1, 7)
        log.complete(oid, FAILED, attempts=3, error="ICEBERG_COMMIT_ERROR")
    ops = read_oplog([tmp_path / "w1-0.jsonl"])
    assert [(o.id, o.outcome, o.attempts, o.snapshot_id) for o in ops] == [
        ("w1-0:1", ACKED, 1, 42),
        ("w1-0:2", FAILED, 3, None),
    ]
    verdict = grade(ops, [EventRow("w1-0", 1, 0, 5)], [], seeded_keys=[])
    assert verdict.valid
    assert verdict.counts()["acked"] == 1


# Without the ledger (only to measure its cost): bounds per key.

BOUNDED = [
    op("w2-0", 1, "counters", 0, 3, ACKED),
    op("w2-0", 2, "counters", 0, 4, UNKNOWN),
    op("w2-1", 1, "counters", 1, 5, FAILED),
]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([(0, 3), (1, 0), (2, 0), (3, 0)], (0, 0, 0)),  # the unknown add did not land
        ([(0, 7), (1, 0), (2, 0), (3, 0)], (0, 0, 0)),  # it did
        ([(0, 0), (1, 0), (2, 0), (3, 0)], (1, 0, 0)),  # the acked add is missing
        ([(0, 10), (1, 0), (2, 0), (3, 0)], (0, 1, 0)),  # an add counted twice
        ([(0, 3), (1, 5), (2, 0), (3, 0)], (0, 1, 0)),  # the failed add applied
        ([(0, 3), (0, 3), (1, 0), (2, 0)], (0, 0, 2)),  # a second row for key 0, key 3 missing
    ],
)
def test_bounds_without_the_ledger(values: list, expected: tuple):
    verdict = grade_bounds(BOUNDED, values, KEYS)
    assert (verdict.below, verdict.above, verdict.wrong_rows) == expected
    assert verdict.valid is (expected == (0, 0, 0))


def test_bounds_need_positive_deltas():
    with pytest.raises(ValueError, match="positive"):
        grade_bounds([op("w2-0", 1, "counters", 0, -1, ACKED)], [(0, 0)], [0])
