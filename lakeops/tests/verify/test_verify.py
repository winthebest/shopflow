"""verify on a stand-in for Trino's metadata tables: the operation's snapshot found by its query id, the checks by op
class and table kind, and when a rollback is safe."""

import re

import pytest

from lakeops.verify import TableKind, verify

TABLE = "lab_exp.e.t"
KEYED = TableKind(key_columns=("id",))
BRONZE = TableKind(external_writer=True)


class FakeTrino:
    """Answers the four statements verify sends: the op's snapshots, a count at a snapshot, the duplicate keys at a
    snapshot, and main's current snapshot."""

    def __init__(self, snapshots: list, counts: dict[int, int], duplicates: int = 0, main: int | None = None) -> None:
        self.snapshots, self.counts, self.duplicates, self.main = snapshots, counts, duplicates, main
        self.statements: list[str] = []

    def cursor(self) -> "FakeTrino":
        return self

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if '"t$snapshots"' in statement:
            self._rows = self.snapshots
        elif "GROUP BY" in statement:
            self._rows = [[self.duplicates]]
        elif (
            m := re.fullmatch(r"SELECT count\(\*\) FROM lab_exp\.e\.t FOR VERSION AS OF (\d+)", statement)
        ) is not None:
            self._rows = [[self.counts[int(m.group(1))]]]
        elif '"t$refs"' in statement:
            self._rows = [[self.main]] if self.main is not None else []
        else:
            raise AssertionError(statement)

    def fetchall(self) -> list:
        return self._rows


def snapshot(snapshot_id: int, parent_id: int | None, operation: str, **summary: str) -> list:
    return [snapshot_id, parent_id, operation, {"trino_query_id": "q1", **summary}]


def test_a_clean_append_passes_and_may_be_rolled_back():
    trino = FakeTrino([snapshot(2, 1, "append", **{"added-records": "3"})], {1: 10, 2: 13}, main=2)
    result = verify(trino, TABLE, query_id="q1", op="append", kind=KEYED)
    assert result.ok
    assert (result.snapshot_id, result.parent_id, result.rollback_safe) == (2, 1, True)
    assert "element_at(summary, 'trino_query_id') = 'q1'" in trino.statements[0]


def test_an_operation_without_a_snapshot_did_not_commit():
    result = verify(FakeTrino([], {}), TABLE, query_id="q1", op="append", kind=KEYED)
    assert [f.check for f in result.findings] == ["snapshot"]
    assert (result.snapshot_id, result.rollback_safe) == (None, False)


def test_an_append_that_deleted_or_miscounted_is_flagged():
    trino = FakeTrino(
        [snapshot(2, 1, "overwrite", **{"added-records": "3", "added-position-deletes": "1"})], {1: 10, 2: 12}
    )
    result = verify(trino, TABLE, query_id="q1", op="append", kind=TableKind())
    assert [f.check for f in result.findings] == ["append", "append"]


def test_a_rewrite_must_keep_the_rows_and_is_never_rolled_back():
    good = verify(
        FakeTrino([snapshot(3, 2, "replace")], {2: 50, 3: 50}, main=3), TABLE, query_id="q1", op="rewrite", kind=KEYED
    )
    assert good.ok
    assert not good.rollback_safe
    assert "never rolled back" in good.rollback_reason
    bad = verify(
        FakeTrino([snapshot(3, 2, "replace")], {2: 50, 3: 49}, main=3), TABLE, query_id="q1", op="rewrite", kind=KEYED
    )
    assert [f.detail for f in bad.findings] == ["a rewrite changed the rows by -1 (50 → 49)"]


@pytest.mark.parametrize(("after", "ok"), [(95, True), (100, True), (90, True), (89, False), (101, False)])
def test_dml_must_stay_within_the_plans_bounds(after: int, ok: bool):
    trino = FakeTrino([snapshot(4, 3, "overwrite")], {3: 100, 4: after}, main=4)
    assert verify(trino, TABLE, query_id="q1", op="dml", kind=KEYED, effect_bounds=(-10, 0)).ok is ok


def test_a_keyed_table_with_a_duplicate_key_is_flagged_an_append_only_one_is_not():
    snapshots = [snapshot(2, 1, "append", **{"added-records": "1"})]
    keyed = verify(FakeTrino(snapshots, {1: 1, 2: 2}, duplicates=1), TABLE, query_id="q1", op="append", kind=KEYED)
    assert [f.check for f in keyed.findings] == ["keys"]
    bronze = FakeTrino(snapshots, {1: 1, 2: 2}, duplicates=1)
    assert verify(bronze, TABLE, query_id="q1", op="append", kind=TableKind()).ok
    assert not any("GROUP BY" in s for s in bronze.statements)


def test_no_rollback_after_another_commit_or_on_a_table_with_an_external_writer():
    snapshots = [snapshot(2, 1, "append", **{"added-records": "1"})]
    later = verify(FakeTrino(snapshots, {1: 1, 2: 2}, main=5), TABLE, query_id="q1", op="append", kind=KEYED)
    assert not later.rollback_safe
    assert "main is at 5" in later.rollback_reason
    bronze = verify(FakeTrino(snapshots, {1: 1, 2: 2}, main=2), TABLE, query_id="q1", op="append", kind=BRONZE)
    assert not bronze.rollback_safe
    assert "external writer" in bronze.rollback_reason
    first = verify(
        FakeTrino([snapshot(1, None, "append", **{"added-records": "2"})], {1: 2}, main=1),
        TABLE,
        query_id="q1",
        op="append",
        kind=KEYED,
    )
    assert first.ok
    assert not first.rollback_safe


def test_two_snapshots_for_one_query_are_reported():
    snapshots = [snapshot(2, 1, "append", **{"added-records": "1"}), snapshot(3, 2, "append", **{"added-records": "1"})]
    result = verify(FakeTrino(snapshots, {2: 1, 3: 2}, main=3), TABLE, query_id="q1", op="append", kind=KEYED)
    assert [f.check for f in result.findings] == ["snapshot"]
    assert result.snapshot_id == 3


@pytest.mark.parametrize(
    "call",
    [
        lambda t: verify(t, "e.t", query_id="q1", op="append", kind=KEYED),
        lambda t: verify(t, TABLE, query_id="q1'; --", op="append", kind=KEYED),
        lambda t: verify(t, TABLE, query_id="q1", op="truncate", kind=KEYED),
        lambda t: verify(t, TABLE, query_id="q1", op="dml", kind=KEYED),
        lambda t: verify(t, TABLE, query_id="q1", op="append", kind=TableKind(key_columns=("id; DROP",))),
    ],
)
def test_bad_inputs_are_refused_before_any_statement(call):
    trino = FakeTrino([], {})
    with pytest.raises(ValueError):
        call(trino)
    assert trino.statements == []
