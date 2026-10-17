"""The idempotent statements, exactly; and the inputs they refuse."""

from datetime import date, datetime

import pytest

from lakeops.writes import add_once, insert_if_absent


def test_insert_if_absent_matches_on_the_key_and_inserts_only_when_missing():
    statement = insert_if_absent(
        "lab_exp.lab_e7.events",
        key={"writer_id": "w1-0", "seq": 17, "key": 5, "event_day": date(2026, 1, 2)},
        values={"delta": 3},
    )
    assert statement == (
        "MERGE INTO lab_exp.lab_e7.events t USING (VALUES ('w1-0', 17, 5, DATE '2026-01-02', 3)) "
        "AS s(writer_id, seq, key, event_day, delta) "
        "ON t.writer_id = s.writer_id AND t.seq = s.seq AND t.key = s.key AND t.event_day = s.event_day "
        "WHEN NOT MATCHED THEN INSERT (writer_id, seq, key, event_day, delta) "
        "VALUES (s.writer_id, s.seq, s.key, s.event_day, s.delta)"
    )


def test_add_once_skips_a_row_whose_ledger_has_the_op():
    statement = add_once(
        "lab_exp.lab_e7.counters",
        key_column="key",
        key=5,
        value_column="value",
        delta=3,
        ledger_column="applied",
        op_id="w2-1:9",
    )
    assert statement == (
        "MERGE INTO lab_exp.lab_e7.counters t USING (VALUES (5, 3, 'w2-1:9')) AS s(key, delta, op) "
        "ON t.key = s.key WHEN MATCHED AND NOT contains(t.applied, s.op) "
        "THEN UPDATE SET value = t.value + s.delta, applied = t.applied || s.op"
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda: insert_if_absent("events", {"id": 1}, {}),
        lambda: insert_if_absent("a.b.c; DROP", {"id": 1}, {}),
        lambda: insert_if_absent("a.b.c", {}, {"v": 1}),
        lambda: insert_if_absent("a.b.c", {"id": 1}, {"id": 2}),
        lambda: insert_if_absent("a.b.c", {"Id": 1}, {}),
        lambda: insert_if_absent("a.b.c", {"id": "x' OR '1'='1"}, {}),
        lambda: insert_if_absent("a.b.c", {"id": True}, {}),
        lambda: insert_if_absent("a.b.c", {"id": 1.5}, {}),
        lambda: insert_if_absent("a.b.c", {"day": datetime(2026, 1, 1)}, {}),
        lambda: add_once("a.b.c", key_column="k", key=1, value_column="v", delta=1, ledger_column="l", op_id="a'; --"),
    ],
)
def test_anything_outside_the_accepted_shapes_is_refused(call):
    with pytest.raises((ValueError, TypeError)):
        call()
