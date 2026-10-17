"""The op log's own rules: what reads back as unknown, what is a runner bug."""

from pathlib import Path

import pytest
from e7_concurrency.oplog import OpLogError, OpLogWriter, Outcome, read_oplog


def write(path: Path, *lines: str, end: str = "\n") -> Path:
    path.write_text("\n".join(lines) + end, encoding="utf-8")
    return path


INVOKE = '{"type":"invoke","op":"w1-0:1","table":"events","key":0,"delta":1,"group":null,"at":0}'
COMPLETE = (
    '{"type":"complete","op":"w1-0:1","outcome":"acked","attempts":1,"error":null,"latency_ms":1.0,'
    '"query_ids":[],"snapshot_id":null}'
)


def test_invoke_without_complete_is_unknown(tmp_path: Path):
    [op] = read_oplog([write(tmp_path / "a", INVOKE)])
    assert (op.outcome, op.error, op.attempts) == (Outcome.UNKNOWN, "no-completion", 0)


def test_torn_last_complete_leaves_the_op_unknown(tmp_path: Path):
    [op] = read_oplog([write(tmp_path / "a", INVOKE, COMPLETE[:40], end="")])
    assert op.outcome is Outcome.UNKNOWN


def test_torn_last_invoke_is_an_op_never_sent(tmp_path: Path):
    assert read_oplog([write(tmp_path / "a", INVOKE[:30], end="")]) == []


def test_malformed_line_before_the_end_is_an_error(tmp_path: Path):
    with pytest.raises(OpLogError, match=":1: not a JSON record"):
        read_oplog([write(tmp_path / "a", INVOKE[:30], COMPLETE)])


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ((COMPLETE,), "completed without an invoke"),
        ((INVOKE, INVOKE), "invoked twice"),
        ((INVOKE, COMPLETE, COMPLETE), "completed twice"),
        ((INVOKE.replace('"events"', '"orders"'),), "unknown table"),
        (('{"type":"retry","op":"w1-0:1"}',), "unknown record type"),
    ],
)
def test_runner_bugs_are_errors(tmp_path: Path, lines: tuple[str, ...], message: str):
    with pytest.raises(OpLogError, match=message):
        read_oplog([write(tmp_path / "a", *lines)])


def test_op_id_reused_across_writer_files_is_an_error(tmp_path: Path):
    with pytest.raises(OpLogError, match="invoked twice"):
        read_oplog([write(tmp_path / "a", INVOKE), write(tmp_path / "b", INVOKE)])


def test_writer_ids_cannot_hold_the_separator(tmp_path: Path):
    with pytest.raises(OpLogError, match="must not contain"):
        OpLogWriter(tmp_path / "a", "w:1")


def test_groups_and_outcomes_round_trip(tmp_path: Path):
    with OpLogWriter(tmp_path / "sink", "sink") as log:
        ids = [log.invoke(seq, "events", seq, 1, group="sink:commit-1") for seq in range(2)]
        for oid in ids:
            log.complete(oid, Outcome.UNKNOWN, attempts=2, error="client-timeout")
    ops = read_oplog([tmp_path / "sink"])
    assert [(o.id, o.commit, o.outcome, o.error) for o in ops] == [
        ("sink:0", "sink:commit-1", Outcome.UNKNOWN, "client-timeout"),
        ("sink:1", "sink:commit-1", Outcome.UNKNOWN, "client-timeout"),
    ]
