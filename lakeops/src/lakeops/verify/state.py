"""`verify_state`: checks a change that commits no snapshot carrying its query id, from two `lakeops.state` captures.

Those changes are history changes (expire_snapshots, rollback_to_snapshot) and definition changes (columns,
properties). Neither adds a snapshot, so the snapshot-based `verify` cannot find them. The executor or guard captures
the table just before and just after the change and compares the two against what the approved plan expected. This
is a pure function, so it is tested without Trino.

- `ExpectExpire(removed)`: the snapshots removed are a subset of `removed`. Iceberg may keep more, e.g. for
  `history.expire.min-snapshots-to-keep`; whether enough went is the scorer's question, not verify's. Rows,
  checksum and refs are unchanged.
- `ExpectRollback(target, rows, checksum)`: main is at `target`, the rows and checksum are the ones read at
  `target` when the evidence was gathered, and no snapshot was removed.
- `ExpectDefinition(...)`: the columns after are exactly the columns before, with the expected drops, renames,
  type changes and additions (added columns go last). The properties are exactly the ones before with the expected
  sets and unsets. The rows are unchanged, and the checksum too when the columns are. No snapshot is added or removed.

Always: a snapshot in `after` that is not in `before` was committed by someone else between the captures. That is a
`concurrent-commit` finding, and the other checks may then differ for that reason alone. Neither class is ever
rolled back here (`rollback_safe` is False): a history change is not undone automatically, and a breaking definition
change is restored from its backup by the guard.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from lakeops.state import TableState

from .checks import Finding, Verification

# $properties keys that change with commits, not with a definition change.
VOLATILE_PROPERTIES = frozenset({"current-snapshot-id"})


@dataclass(frozen=True)
class ExpectExpire:
    removed: frozenset[int]


@dataclass(frozen=True)
class ExpectRollback:
    target: int
    rows: int
    checksum: str


@dataclass(frozen=True)
class ExpectDefinition:
    columns_added: tuple[tuple[str, str], ...] = ()  # (name, type), in the order added
    columns_dropped: frozenset[str] = frozenset()
    renamed: Mapping[str, str] = field(default_factory=dict)  # old name -> new name
    type_changed: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # name (after renames) -> (old, new)
    properties_set: Mapping[str, str] = field(default_factory=dict)
    properties_unset: frozenset[str] = frozenset()


def _main(state: TableState) -> int | None:
    return next((ref.snapshot_id for ref in state.refs if ref.name == "main"), None)


def _refs(state: TableState) -> dict[str, int]:
    return {ref.name: ref.snapshot_id for ref in state.refs}


def _expected_columns(before: TableState, expected: ExpectDefinition) -> tuple[list[tuple[str, str]], list[str]]:
    """The columns `before` becomes under the plan, and any way the plan does not fit `before`."""
    names = [name for name, _ in before.columns]
    misfits = [f"drop {c}: no such column" for c in sorted(expected.columns_dropped - set(names))]
    misfits += [f"rename {c}: no such column" for c in sorted(set(expected.renamed) - set(names))]
    columns = []
    for name, kind in before.columns:
        if name in expected.columns_dropped:
            continue
        name = expected.renamed.get(name, name)
        if name in expected.type_changed:
            old, new = expected.type_changed[name]
            if old != kind:
                misfits.append(f"type of {name}: the plan says {old}, the table had {kind}")
            kind = new
        columns.append((name, kind))
    misfits += [f"type of {c}: no such column" for c in sorted(set(expected.type_changed) - {c for c, _ in columns})]
    return [*columns, *expected.columns_added], misfits


def verify_state(
    before: TableState,
    after: TableState,
    *,
    op: Literal["history", "definition"],
    expected: ExpectExpire | ExpectRollback | ExpectDefinition,
) -> Verification:
    if before.table != after.table:
        raise ValueError(f"captures of two tables: {before.table} and {after.table}")
    valid = {"history": (ExpectExpire, ExpectRollback), "definition": (ExpectDefinition,)}
    if op not in valid or not isinstance(expected, valid[op]):
        raise ValueError(f"op {op!r} does not take {type(expected).__name__}")

    findings = []
    before_ids = {s.snapshot_id for s in before.snapshots}
    after_ids = {s.snapshot_id for s in after.snapshots}
    added, removed = sorted(after_ids - before_ids), before_ids - after_ids
    if added:
        findings.append(Finding("concurrent-commit", f"snapshots committed between the captures: {added}"))

    if isinstance(expected, ExpectExpire):
        unplanned = sorted(removed - expected.removed)
        if unplanned:
            findings.append(Finding("history", f"removed snapshots the plan did not list: {unplanned}"))
        if (after.rows, after.checksum) != (before.rows, before.checksum):
            findings.append(Finding("history", f"an expire changed the rows: {before.rows} → {after.rows}"))
        if _refs(after) != _refs(before):
            findings.append(Finding("history", f"an expire moved refs: {_refs(before)} → {_refs(after)}"))
    elif isinstance(expected, ExpectRollback):
        if _main(after) != expected.target:
            findings.append(Finding("history", f"main is at {_main(after)}, the plan rolls back to {expected.target}"))
        if (after.rows, after.checksum) != (expected.rows, expected.checksum):
            findings.append(
                Finding("history", f"rows after the rollback are {after.rows}, the target had {expected.rows}")
            )
        if removed:
            findings.append(Finding("history", f"a rollback removed snapshots: {sorted(removed)}"))
    else:
        columns, misfits = _expected_columns(before, expected)
        findings += [Finding("definition", f"the plan does not fit the table: {m}") for m in misfits]
        if list(after.columns) != columns:
            findings.append(Finding("definition", f"columns are {list(after.columns)}, the plan gives {columns}"))
        properties = {k: v for k, v in before.properties if k not in VOLATILE_PROPERTIES}
        properties = {**properties, **expected.properties_set}
        for key in expected.properties_unset:
            properties.pop(key, None)
        actual = {k: v for k, v in after.properties if k not in VOLATILE_PROPERTIES}
        if actual != properties:
            changed = sorted(k for k in actual.keys() | properties.keys() if actual.get(k) != properties.get(k))
            findings.append(Finding("definition", f"properties differ from the plan at: {changed}"))
        if after.rows != before.rows:
            findings.append(
                Finding("definition", f"a definition change changed the rows: {before.rows} → {after.rows}")
            )
        elif before.columns == after.columns and after.checksum != before.checksum:
            findings.append(Finding("definition", "the checksum changed while the columns did not"))
        if removed:
            findings.append(Finding("definition", f"a definition change removed snapshots: {sorted(removed)}"))

    reason = (
        "a history change is never rolled back automatically"
        if op == "history"
        else "a definition change is restored from its backup by the guard, not rolled back"
    )
    return Verification(tuple(findings), _main(after), _main(before), False, reason)
