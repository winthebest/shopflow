"""The checks behind `verify`. The SQL is built only from validated identifiers, query ids and integer snapshot ids."""

import re
from dataclasses import dataclass
from typing import Literal

from trino.dbapi import Connection

Op = Literal["append", "rewrite", "dml"]
OPS = ("append", "rewrite", "dml")

_IDENTIFIER = re.compile(r"[a-z0-9_]+")
_QUERY_ID = re.compile(r"[0-9a-z_]+")
# Summary keys that mean the snapshot deleted rows; an append must have none of them.
_DELETES = ("deleted-records", "added-delete-files", "added-position-deletes", "added-equality-deletes")


@dataclass(frozen=True)
class TableKind:
    key_columns: tuple[str, ...] = ()  # empty: append-only, duplicates allowed (bronze)
    external_writer: bool = False  # someone else commits to it too: never roll it back


@dataclass(frozen=True)
class Finding:
    check: str  # snapshot, append, rewrite, dml, keys
    detail: str


@dataclass(frozen=True)
class Verification:
    findings: tuple[Finding, ...]
    snapshot_id: int | None
    parent_id: int | None
    rollback_safe: bool
    rollback_reason: str

    @property
    def ok(self) -> bool:
        return not self.findings


def _table(table: str) -> tuple[str, str, str]:
    parts = table.split(".")
    if len(parts) != 3 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError(f"table {table!r}: name it catalog.schema.table ([a-z0-9_] each)")
    return parts[0], parts[1], parts[2]


def _rows(conn: Connection, statement: str) -> list:
    cur = conn.cursor()
    cur.execute(statement)
    return cur.fetchall()


def _count(conn: Connection, table: str, snapshot_id: int | None) -> int:
    if snapshot_id is None:
        return 0  # before the table's first snapshot
    return _rows(conn, f"SELECT count(*) FROM {table} FOR VERSION AS OF {int(snapshot_id)}")[0][0]  # noqa: S608


def verify(
    conn: Connection,
    table: str,
    *,
    query_id: str,
    op: Op,
    kind: TableKind,
    effect_bounds: tuple[int, int] | None = None,
) -> Verification:
    catalog, schema, name = _table(table)
    if op not in OPS:
        raise ValueError(f"op {op!r} is not one of {', '.join(OPS)}")
    if op == "dml" and effect_bounds is None:
        raise ValueError("a dml op needs effect_bounds (min, max change in rows)")
    if not _QUERY_ID.fullmatch(query_id):
        raise ValueError(f"not a Trino query id: {query_id!r}")
    for column in kind.key_columns:
        if not _IDENTIFIER.fullmatch(column):
            raise ValueError(f"key column {column!r} is not [a-z0-9_]+")

    snapshots = _rows(
        conn,
        f'SELECT snapshot_id, parent_id, operation, summary FROM {catalog}.{schema}."{name}$snapshots" '  # noqa: S608
        f"WHERE element_at(summary, 'trino_query_id') = '{query_id}' ORDER BY committed_at",
    )
    if not snapshots:
        finding = Finding("snapshot", f"no snapshot of {table} carries query {query_id}: the operation did not commit")
        return Verification((finding,), None, None, False, "nothing to roll back: the operation did not commit")

    findings = []
    if len(snapshots) > 1:
        findings.append(Finding("snapshot", f"{len(snapshots)} snapshots carry query {query_id}; checked the last"))
    snapshot_id, parent_id, _operation, summary = snapshots[-1]
    summary = summary or {}
    before, after = _count(conn, table, parent_id), _count(conn, table, snapshot_id)
    delta = after - before

    if op == "append":
        deletes = {key: summary[key] for key in _DELETES if int(summary.get(key, 0))}
        if deletes:
            findings.append(Finding("append", f"an append deleted rows: {deletes}"))
        added = int(summary.get("added-records", 0))
        if delta != added:
            findings.append(Finding("append", f"rows changed by {delta}, the snapshot says {added} were added"))
    elif op == "rewrite":
        if delta:
            findings.append(Finding("rewrite", f"a rewrite changed the rows by {delta} ({before} → {after})"))
    else:
        low, high = effect_bounds
        if not low <= delta <= high:
            findings.append(Finding("dml", f"rows changed by {delta}, the plan allows [{low}, {high}]"))

    if kind.key_columns:
        columns = ", ".join(kind.key_columns)
        duplicated = _rows(
            conn,
            f"SELECT count(*) FROM (SELECT {columns} FROM {table} FOR VERSION AS OF {int(snapshot_id)} "  # noqa: S608
            f"GROUP BY {columns} HAVING count(*) > 1)",
        )[0][0]
        if duplicated:
            findings.append(Finding("keys", f"{duplicated} key(s) of ({columns}) appear more than once"))

    current = _rows(conn, f"SELECT snapshot_id FROM {catalog}.{schema}.\"{name}$refs\" WHERE name = 'main'")  # noqa: S608
    current_id = current[0][0] if current else None
    if op == "rewrite":
        safe, reason = False, "a rewrite does not change the data's logic and is never rolled back automatically"
    elif kind.external_writer:
        safe, reason = False, "the table has an external writer: a rollback would discard its commits"
    elif current_id != snapshot_id:
        safe, reason = False, f"another commit came after the operation: main is at {current_id}, not {snapshot_id}"
    elif parent_id is None:
        safe, reason = False, "the operation made the table's first snapshot: there is nothing to roll back to"
    else:
        safe, reason = True, f"main is still the operation's snapshot; roll back to {parent_id}"
    return Verification(tuple(findings), snapshot_id, parent_id, safe, reason)
