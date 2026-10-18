"""verify_state on built TableState captures: expire, rollback and definition changes against their plans."""

import dataclasses

import pytest

from lakeops.state import RefEntry, SnapshotEntry, TableState
from lakeops.verify import ExpectDefinition, ExpectExpire, ExpectRollback, verify_state


def state(
    snapshots: tuple[int, ...] = (1, 2, 3),
    main: int = 3,
    rows: int = 10,
    checksum: str = "c10",
    columns: tuple[tuple[str, str], ...] = (("id", "bigint"), ("v", "varchar")),
    properties: tuple[tuple[str, str], ...] = (("current-snapshot-id", "3"), ("format-version", "2")),
) -> TableState:
    entries = tuple(SnapshotEntry(s, s - 1 or None, f"2026-10-18T00:00:0{s}Z", "append") for s in snapshots)
    return TableState(
        table="lab_exp.e.t",
        rows=rows,
        checksum=checksum,
        partitions=(),
        snapshots=entries,
        refs=(RefEntry("main", "BRANCH", main),),
        files=(),
        columns=columns,
        properties=properties,
        definition_sha256="x",
    )


def checks(verification) -> list[str]:
    return [f.check for f in verification.findings]


# History: expire.


def test_an_expire_within_the_plan_passes_and_is_never_rollback_safe():
    result = verify_state(state(), state(snapshots=(3,)), op="history", expected=ExpectExpire(frozenset({1, 2})))
    assert result.ok
    assert not result.rollback_safe


def test_an_expire_that_kept_more_than_planned_still_passes():
    """Iceberg may keep snapshots (min-snapshots-to-keep): whether enough went is the scorer's question."""
    result = verify_state(state(), state(snapshots=(2, 3)), op="history", expected=ExpectExpire(frozenset({1, 2})))
    assert result.ok


def test_an_expire_that_removed_unplanned_snapshots_or_changed_rows_is_flagged():
    result = verify_state(state(), state(snapshots=(3,), rows=9), op="history", expected=ExpectExpire(frozenset({1})))
    assert [f.detail for f in result.findings] == [
        "removed snapshots the plan did not list: [2]",
        "an expire changed the rows: 10 → 9",
    ]


# History: rollback.


def test_a_rollback_to_the_target_with_its_rows_passes():
    after = state(main=2, rows=8, checksum="c8")
    assert verify_state(state(), after, op="history", expected=ExpectRollback(2, 8, "c8")).ok


def test_a_rollback_elsewhere_or_with_other_rows_is_flagged():
    after = state(main=1, rows=8, checksum="c8x")
    assert checks(verify_state(state(), after, op="history", expected=ExpectRollback(2, 8, "c8"))) == [
        "history",
        "history",
    ]


# Definition.

BEFORE_DEF = state(columns=(("id", "bigint"), ("v", "varchar"), ("old", "integer")))


@pytest.mark.parametrize(
    ("after_columns", "expected"),
    [
        (
            (("id", "bigint"), ("v", "varchar"), ("old", "integer"), ("note", "varchar")),
            ExpectDefinition(columns_added=(("note", "varchar"),)),
        ),
        ((("id", "bigint"), ("v", "varchar")), ExpectDefinition(columns_dropped=frozenset({"old"}))),
        ((("id", "bigint"), ("value", "varchar"), ("old", "integer")), ExpectDefinition(renamed={"v": "value"})),
        (
            (("id", "bigint"), ("v", "varchar"), ("old", "bigint")),
            ExpectDefinition(type_changed={"old": ("integer", "bigint")}),
        ),
    ],
)
def test_column_changes_that_match_the_plan_pass(after_columns: tuple, expected: ExpectDefinition):
    after = dataclasses.replace(BEFORE_DEF, columns=after_columns, checksum="other")
    assert verify_state(BEFORE_DEF, after, op="definition", expected=expected).ok


def test_a_column_change_beyond_the_plan_is_flagged():
    after = dataclasses.replace(BEFORE_DEF, columns=(("id", "bigint"), ("note", "varchar")))
    result = verify_state(
        BEFORE_DEF, after, op="definition", expected=ExpectDefinition(columns_added=(("note", "varchar"),))
    )
    assert checks(result) == ["definition"]


def test_a_plan_that_does_not_fit_the_table_is_flagged():
    expected = ExpectDefinition(columns_dropped=frozenset({"ghost"}), type_changed={"old": ("varchar", "bigint")})
    result = verify_state(BEFORE_DEF, BEFORE_DEF, op="definition", expected=expected)
    details = [f.detail for f in result.findings]
    assert "the plan does not fit the table: drop ghost: no such column" in details
    assert "the plan does not fit the table: type of old: the plan says varchar, the table had integer" in details


def test_properties_must_match_the_plan_apart_from_the_commit_driven_ones():
    after = state(
        properties=(("current-snapshot-id", "9"), ("format-version", "2"), ("write.target-file-size-bytes", "1024"))
    )
    expected = ExpectDefinition(properties_set={"write.target-file-size-bytes": "1024"})
    assert verify_state(state(), after, op="definition", expected=expected).ok
    result = verify_state(state(), after, op="definition", expected=ExpectDefinition())
    assert [f.detail for f in result.findings] == [
        "properties differ from the plan at: ['write.target-file-size-bytes']"
    ]


def test_a_definition_change_that_changed_rows_or_the_checksum_is_flagged():
    assert checks(verify_state(state(), state(rows=9), op="definition", expected=ExpectDefinition())) == ["definition"]
    result = verify_state(state(), state(checksum="other"), op="definition", expected=ExpectDefinition())
    assert [f.detail for f in result.findings] == ["the checksum changed while the columns did not"]


# Always.


def test_a_snapshot_committed_between_the_captures_is_a_concurrent_commit():
    after = state(snapshots=(1, 2, 3, 4), main=4)
    result = verify_state(state(), after, op="definition", expected=ExpectDefinition())
    assert checks(result)[0] == "concurrent-commit"
    assert (result.snapshot_id, result.parent_id) == (4, 3)


@pytest.mark.parametrize(
    ("op", "expected"),
    [("history", ExpectDefinition()), ("definition", ExpectExpire(frozenset())), ("dml", ExpectDefinition())],
)
def test_the_op_and_its_expectation_must_agree(op: str, expected):
    with pytest.raises(ValueError, match="does not take"):
        verify_state(state(), state(), op=op, expected=expected)


def test_captures_of_two_tables_are_refused():
    with pytest.raises(ValueError, match="two tables"):
        verify_state(
            state(),
            dataclasses.replace(state(), table="lab_exp.e.other"),
            op="history",
            expected=ExpectExpire(frozenset()),
        )
