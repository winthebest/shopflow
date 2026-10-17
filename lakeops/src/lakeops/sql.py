"""Table names and statements for lakeops: Iceberg tables in Trino, named `catalog.schema.table`.

Identifiers cannot be bound as query parameters, so lakeops admits only plain lowercase names ([a-z0-9_]; Trino
lowercases unquoted identifiers anyway) and double-quotes every part it writes into SQL.
"""

import re
from dataclasses import dataclass

import trino

_IDENTIFIER = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class TableName:
    catalog: str
    schema: str
    table: str

    @classmethod
    def parse(cls, name: str) -> "TableName":
        parts = name.split(".")
        if len(parts) != 3 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
            raise ValueError(f"table {name!r}: expected catalog.schema.table, each part [a-z0-9_]+")
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.table}"

    @property
    def sql(self) -> str:
        """The quoted name for SQL."""
        return f'"{self.catalog}"."{self.schema}"."{self.table}"'

    def metadata(self, kind: str) -> str:
        """A metadata table, e.g. metadata("snapshots") -> "catalog"."schema"."table$snapshots"."""
        if not _IDENTIFIER.fullmatch(kind):
            raise ValueError(f"metadata table {kind!r}")
        return f'"{self.catalog}"."{self.schema}"."{self.table}${kind}"'


def table_name(table: "TableName | str") -> TableName:
    return table if isinstance(table, TableName) else TableName.parse(table)


def query(conn: trino.dbapi.Connection, sql: str) -> list:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def is_partitioned(conn: trino.dbapi.Connection, table: "TableName | str") -> bool:
    """Whether the table has a partition spec: Trino gives `$files` a `partition` column only then."""
    return any(row[0] == "partition" for row in query(conn, f"DESCRIBE {table_name(table).metadata('files')}"))
