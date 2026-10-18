"""A table's fingerprint: what its rows are and how its current snapshot stores them, to tell two states apart.

- Content: the row count and an order-insensitive checksum over every column (Trino's `checksum` aggregate of a ROW
  of all columns); a clone with the same rows has the same content. `checksum_at` gives the same at any snapshot
  (`FOR VERSION AS OF`), e.g. an operation's own snapshot and its parent.
- Layout of the current snapshot: data files, delete files, manifests (`$files`, `$manifests`).
- History: snapshots and refs. A rollback restores content and layout, while history keeps the snapshots after it,
  so `same_state` compares content and layout only.
"""

from dataclasses import dataclass

import trino

from lakeops.sql import TableName, query, table_name


@dataclass(frozen=True)
class Fingerprint:
    rows: int
    checksum: str  # hex; empty for a table without rows
    data_files: int
    delete_files: int
    manifests: int
    snapshots: int
    refs: tuple[str, ...]

    def same_state(self, other: "Fingerprint") -> bool:
        """Same rows stored the same way (content and the current snapshot's layout); history may differ."""
        return (self.rows, self.checksum, self.data_files, self.delete_files, self.manifests) == (
            other.rows,
            other.checksum,
            other.data_files,
            other.delete_files,
            other.manifests,
        )

    def same_rows(self, other: "Fingerprint") -> bool:
        return (self.rows, self.checksum) == (other.rows, other.checksum)


def columns(conn: trino.dbapi.Connection, table: TableName | str) -> list[str]:
    t = table_name(table)
    rows = query(
        conn,
        f'SELECT column_name FROM "{t.catalog}".information_schema.columns '  # noqa: S608
        f"WHERE table_schema = '{t.schema}' AND table_name = '{t.table}' ORDER BY ordinal_position",
    )
    if not rows:
        raise LookupError(f"no columns for {t}: the table does not exist or is not visible")
    return [row[0] for row in rows]


def checksum_at(
    conn: trino.dbapi.Connection, table: TableName | str, snapshot_id: int | None = None
) -> tuple[int, str]:
    """Rows and checksum of the table now, or at `snapshot_id`; "" for no rows.

    The columns are the current schema's: a snapshot from before a column was added or dropped cannot be read this way
    (Trino reports the missing column).
    """
    t = table_name(table)
    row = ", ".join('"' + c.replace('"', '""') + '"' for c in columns(conn, t))
    version = "" if snapshot_id is None else f" FOR VERSION AS OF {int(snapshot_id)}"
    rows, checksum = query(conn, f"SELECT count(*), to_hex(checksum(ROW({row}))) FROM {t.sql}{version}")[0]  # noqa: S608
    return rows, checksum or ""


def fingerprint(conn: trino.dbapi.Connection, table: TableName | str) -> Fingerprint:
    t = table_name(table)
    rows, checksum = checksum_at(conn, t)
    data_files, delete_files = query(
        conn,
        f"SELECT count_if(content = 0), count_if(content <> 0) FROM {t.metadata('files')}",  # noqa: S608
    )[0]
    manifests = query(conn, f"SELECT count(*) FROM {t.metadata('manifests')}")[0][0]  # noqa: S608
    snapshots = query(conn, f"SELECT count(*) FROM {t.metadata('snapshots')}")[0][0]  # noqa: S608
    refs = tuple(r[0] for r in query(conn, f"SELECT name FROM {t.metadata('refs')} ORDER BY name"))  # noqa: S608
    return Fingerprint(rows, checksum, data_files or 0, delete_files or 0, manifests, snapshots, refs)
