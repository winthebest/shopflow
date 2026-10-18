"""The full state of Iceberg tables, captured through Trino for whoever judges a run (a library: no verdicts here).

One TableState per table:
- rows and checksum over the whole table (the order-independent checksum of lakeops.fingerprint);
- per partition: its key, rows and checksum. The key is Iceberg's partition path, `field=value/field=value` in
  partition-spec order (e.g. `orderdate_month=1992-01`); an unpartitioned table has the one key "";
- `$snapshots` (snapshot_id, parent_id, committed_at, operation), `$refs` (name, type, snapshot_id) and `$files`
  (path, content, record_count, file_size_in_bytes) of the current snapshot;
- the definition: columns with their types, `$properties` (key, value), and the sha256 of SHOW CREATE TABLE, so a
  column added, dropped or renamed and a property set (e.g. a retention knob) show even when no row changes.

`capture_namespace` captures every table of a namespace; states are JSON-serializable (to_json / from_json).
"""

import hashlib
from dataclasses import asdict, dataclass

import trino

from lakeops.fingerprint import columns
from lakeops.sql import TableName, is_partitioned, query, table_name


@dataclass(frozen=True)
class PartitionState:
    partition: str
    rows: int
    checksum: str


@dataclass(frozen=True)
class SnapshotEntry:
    snapshot_id: int
    parent_id: int | None
    committed_at: str  # ISO 8601, as Trino returns it
    operation: str


@dataclass(frozen=True)
class RefEntry:
    name: str
    type: str
    snapshot_id: int


@dataclass(frozen=True)
class FileEntry:
    path: str
    content: int  # 0 data, 1 position deletes, 2 equality deletes
    record_count: int
    file_size_in_bytes: int


@dataclass(frozen=True)
class TableState:
    table: str
    rows: int
    checksum: str
    partitions: tuple[PartitionState, ...]
    snapshots: tuple[SnapshotEntry, ...]
    refs: tuple[RefEntry, ...]
    files: tuple[FileEntry, ...]
    columns: tuple[tuple[str, str], ...]  # (name, type) in ordinal order
    properties: tuple[tuple[str, str], ...]  # $properties (key, value), sorted by key
    definition_sha256: str  # of SHOW CREATE TABLE

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "TableState":
        return cls(
            table=data["table"],
            rows=data["rows"],
            checksum=data["checksum"],
            partitions=tuple(PartitionState(**p) for p in data["partitions"]),
            snapshots=tuple(SnapshotEntry(**s) for s in data["snapshots"]),
            refs=tuple(RefEntry(**r) for r in data["refs"]),
            files=tuple(FileEntry(**f) for f in data["files"]),
            columns=tuple(tuple(c) for c in data["columns"]),
            properties=tuple(tuple(p) for p in data["properties"]),
            definition_sha256=data["definition_sha256"],
        )


def partition_key(value: object) -> str:
    """Iceberg's partition path from Trino's `$partition` value; "" for an unpartitioned table."""
    return "" if value is None else str(value)


def capture(conn: trino.dbapi.Connection, table: TableName | str) -> TableState:
    t = table_name(table)
    row = "ROW(" + ", ".join('"' + c.replace('"', '""') + '"' for c in columns(conn, t)) + ")"
    key = '"$partition"' if is_partitioned(conn, t) else "CAST(NULL AS varchar)"  # unpartitioned: the one key ""
    partitions = tuple(
        PartitionState(partition_key(p), rows, checksum or "")
        for p, rows, checksum in query(
            conn,
            f"SELECT {key}, count(*), to_hex(checksum({row})) FROM {t.sql} GROUP BY 1 ORDER BY 1",  # noqa: S608
        )
    )
    snapshots = tuple(
        SnapshotEntry(sid, parent, committed.isoformat(), op)
        for sid, parent, committed, op in query(
            conn,
            f"SELECT snapshot_id, parent_id, committed_at, operation FROM {t.metadata('snapshots')} "  # noqa: S608
            "ORDER BY committed_at, snapshot_id",
        )
    )
    refs = tuple(
        RefEntry(*r)
        for r in query(conn, f"SELECT name, type, snapshot_id FROM {t.metadata('refs')} ORDER BY name")  # noqa: S608
    )
    files = tuple(
        FileEntry(*f)
        for f in query(
            conn,
            f"SELECT file_path, content, record_count, file_size_in_bytes FROM {t.metadata('files')} "  # noqa: S608
            "ORDER BY file_path",
        )
    )
    rows, checksum = query(conn, f"SELECT count(*), to_hex(checksum({row})) FROM {t.sql}")[0]  # noqa: S608
    typed = tuple(
        (name, data_type)
        for name, data_type in query(
            conn,
            f'SELECT column_name, data_type FROM "{t.catalog}".information_schema.columns '  # noqa: S608
            f"WHERE table_schema = '{t.schema}' AND table_name = '{t.table}' ORDER BY ordinal_position",
        )
    )
    properties = tuple(
        (key, value)
        for key, value in query(conn, f"SELECT key, value FROM {t.metadata('properties')} ORDER BY key")  # noqa: S608
    )
    definition = query(conn, f"SHOW CREATE TABLE {t.sql}")[0][0]
    return TableState(
        str(t),
        rows,
        checksum or "",
        partitions,
        snapshots,
        refs,
        files,
        typed,
        properties,
        hashlib.sha256(definition.encode()).hexdigest(),
    )


def capture_namespace(conn: trino.dbapi.Connection, catalog: str, namespace: str) -> dict[str, TableState]:
    """Every base table of `catalog.namespace`, by table name."""
    probe = TableName(catalog, namespace, "probe")  # validates both names
    names = query(
        conn,
        f'SELECT table_name FROM "{probe.catalog}".information_schema.tables '  # noqa: S608
        f"WHERE table_schema = '{probe.schema}' AND table_type = 'BASE TABLE' ORDER BY table_name",
    )
    return {name: capture(conn, TableName(catalog, namespace, name)) for (name,) in names}
