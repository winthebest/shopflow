"""Table states for maintenance experiments: the same rows stored four ways, built from tpch.<scale>.orders.

Fixed for every experiment (docs/research/protocol.md §4): scale `tiny` (15 000 orders, 1992-01 to 1998-08, 80
months), Iceberg v2, Parquet, partitioning month(orderdate), BATCHES = 20. The states differ only in how the rows are
stored, never in which rows:

- healthy: one CREATE TABLE AS: few files per partition;
- fragmented: BATCHES inserts (orderkey % BATCHES), each touching every partition: BATCHES small files per partition;
- skewed: the rows of the HOT busiest months in BATCHES inserts, the rest in one: small files crowd a few partitions;
- mixed: healthy, then a tenth of the rows (orderkey % 10 = 0) deleted and inserted again in BATCHES inserts: delete
  files and small files on top of a healthy base.

Every state holds exactly the rows of tpch.<scale>.orders, so all states share one lakeops fingerprint (rows and
checksum); only their files, delete files and snapshots differ.
"""

import re

import trino

from lakeops.sql import TableName, query, table_name

SCALE = "tiny"
PARTITIONING = "month(orderdate)"
BATCHES = 20
HOT = 3
STATES = ("healthy", "fragmented", "skewed", "mixed")


def source(scale: str = SCALE) -> str:
    if not re.fullmatch(r"tiny|sf[0-9]+", scale):
        raise ValueError(f"tpch scale {scale!r}: tiny or sf<N>")
    return f'"tpch"."{scale}"."orders"'


def build(
    conn: trino.dbapi.Connection,
    state: str,
    target: TableName | str,
    *,
    scale: str = SCALE,
    batches: int = BATCHES,
) -> TableName:
    """Creates `target` in `state` (see the module docstring); returns its name."""
    if state not in STATES:
        raise ValueError(f"state {state!r}: one of {STATES}")
    t, src = table_name(target), source(scale)
    create = (
        f"CREATE TABLE {t.sql} WITH (format_version = 2, format = 'PARQUET', partitioning = ARRAY['{PARTITIONING}'])"
    )
    if state in ("healthy", "mixed"):
        query(conn, f"{create} AS SELECT * FROM {src}")  # noqa: S608
    else:
        query(conn, f"{create} AS SELECT * FROM {src} WITH NO DATA")  # noqa: S608
    if state == "fragmented":
        for b in range(batches):
            query(conn, f"INSERT INTO {t.sql} SELECT * FROM {src} WHERE orderkey % {batches} = {b}")  # noqa: S608
    elif state == "skewed":
        busiest = f"SELECT date_trunc('month', orderdate) FROM {src} GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT {HOT}"  # noqa: S608
        hot = [r[0] for r in query(conn, busiest)]
        months = ", ".join(f"DATE '{m.isoformat()}'" for m in hot)
        is_hot = f"date_trunc('month', orderdate) IN ({months})"
        query(conn, f"INSERT INTO {t.sql} SELECT * FROM {src} WHERE NOT ({is_hot})")  # noqa: S608
        for b in range(batches):
            where = f"{is_hot} AND orderkey % {batches} = {b}"
            query(conn, f"INSERT INTO {t.sql} SELECT * FROM {src} WHERE {where}")  # noqa: S608
    elif state == "mixed":
        query(conn, f"DELETE FROM {t.sql} WHERE orderkey % 10 = 0")  # noqa: S608
        for b in range(batches):
            where = f"orderkey % 10 = 0 AND (orderkey / 10) % {batches} = {b}"
            query(conn, f"INSERT INTO {t.sql} SELECT * FROM {src} WHERE {where}")  # noqa: S608
    return t
