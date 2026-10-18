"""Snapshots of an Iceberg table through Trino: its history and refs, a clone with the same layout, a rollback.

- `snapshots` and `refs` read the `$snapshots` and `$refs` metadata tables.
- `clone` copies a table into a new one: `CREATE TABLE … (LIKE …) WITH (…)` with the source's columns and every table
  property of its SHOW CREATE TABLE (partitioning, sort order, file format, format version, …) except `location`,
  then `INSERT … SELECT`. `INCLUDING PROPERTIES` would copy the location too, and Polaris refuses a second table at
  the first one's location (Trino reports "Failed to create transaction"; checked on the lab, 2026-10-18). The clone
  gets its own location and has the source's current rows, not its history.
- `rollback` sets the current snapshot back (`rollback_to_snapshot`). History stays: the snapshots after it remain in
  `$snapshots` until they expire.
"""

import re
from dataclasses import dataclass
from datetime import datetime

import trino

from lakeops.sql import TableName, query, table_name


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: int
    parent_id: int | None
    committed_at: datetime
    operation: str
    summary: dict[str, str]


@dataclass(frozen=True)
class Ref:
    name: str
    type: str  # BRANCH or TAG
    snapshot_id: int
    max_reference_age_ms: int | None


def snapshots(conn: trino.dbapi.Connection, table: TableName | str) -> list[Snapshot]:
    """Every snapshot still in the table's metadata, oldest first."""
    t = table_name(table)
    rows = query(
        conn,
        f"SELECT snapshot_id, parent_id, committed_at, operation, summary FROM {t.metadata('snapshots')} "  # noqa: S608
        "ORDER BY committed_at, snapshot_id",
    )
    return [Snapshot(r[0], r[1], r[2], r[3], dict(r[4] or {})) for r in rows]


def refs(conn: trino.dbapi.Connection, table: TableName | str) -> list[Ref]:
    t = table_name(table)
    rows = query(
        conn,
        f"SELECT name, type, snapshot_id, max_reference_age_in_ms FROM {t.metadata('refs')} ORDER BY name",  # noqa: S608
    )
    return [Ref(*row) for row in rows]


def current_snapshot_id(conn: trino.dbapi.Connection, table: TableName | str) -> int | None:
    """The snapshot the table reads now (its `main` branch); None for a table without a snapshot."""
    return next((ref.snapshot_id for ref in refs(conn, table) if ref.name == "main"), None)


def clone(conn: trino.dbapi.Connection, source: TableName | str, target: TableName | str) -> TableName:
    """A new table `target` with the layout and current rows of `source` (see the module docstring)."""
    s, t = table_name(source), table_name(target)
    if s.catalog != t.catalog:
        raise ValueError(f"clone within one catalog: {s} -> {t} (LIKE reads the source through the target's catalog)")
    properties = {k: v for k, v in table_properties(conn, s).items() if k != "location"}
    with_clause = f" WITH ({', '.join(f'{k} = {v}' for k, v in properties.items())})" if properties else ""
    query(conn, f"CREATE TABLE {t.sql} (LIKE {s.sql}){with_clause}")
    query(conn, f"INSERT INTO {t.sql} SELECT * FROM {s.sql}")  # noqa: S608
    return t


def table_properties(conn: trino.dbapi.Connection, table: TableName | str) -> dict[str, str]:
    """The WITH properties of SHOW CREATE TABLE, as SQL text (e.g. format: 'PARQUET', partitioning: ARRAY[…])."""
    shown = query(conn, f"SHOW CREATE TABLE {table_name(table).sql}")[0][0]
    clause = shown.split("\nWITH (\n", 1)[1].rsplit("\n)", 1)[0] if "\nWITH (\n" in shown else ""
    return dict(re.findall(r"^\s+(\w+) = (.*?),?$", clause, re.MULTILINE))


def rollback(conn: trino.dbapi.Connection, table: TableName | str, snapshot_id: int) -> None:
    t = table_name(table)
    query(conn, f"ALTER TABLE {t.sql} EXECUTE rollback_to_snapshot(snapshot_id => {int(snapshot_id)})")
