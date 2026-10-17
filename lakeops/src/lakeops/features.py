"""A table's maintenance features, read from its metadata tables through Trino (no data scan except for refs).

Of the current snapshot (`$files`): data files per partition (max, mean), the share of small data files, the
coefficient of variation of data file sizes, delete files by kind, bytes. Of the history (`$snapshots`,
`$all_manifests`, `$all_entries`): snapshots, manifests, and the bytes of data and delete files that only older
snapshots still reference (what expiring them would free). Of the refs (`$refs`): the bytes of data files that a ref
other than `main` keeps alive and the current snapshot does not reference (what removing those refs would free, once
expired); read with `FOR VERSION AS OF` on the ref's snapshot.
"""

from dataclasses import dataclass

import trino

from lakeops.sql import TableName, is_partitioned, query, table_name

SMALL_FILE_BYTES = 32 * 2**20  # a data file under 32 MiB counts as small (Trino writes up to 1 GiB by default)


@dataclass(frozen=True)
class Features:
    partitions: int
    data_files: int
    max_files_per_partition: int
    avg_files_per_partition: float
    small_file_ratio: float
    file_size_cv: float
    data_bytes: int
    position_delete_files: int
    equality_delete_files: int
    snapshots: int
    manifests: int  # of the current snapshot
    all_manifests: int  # referenced by any snapshot
    history_bytes: int  # data and delete files referenced only by older snapshots
    ref_pinned_bytes: int  # data files kept alive only by refs other than main


def features(
    conn: trino.dbapi.Connection, table: TableName | str, small_file_bytes: int = SMALL_FILE_BYTES
) -> Features:
    t = table_name(table)
    files = t.metadata("files")
    # An unpartitioned table is one partition (its $files has no partition column).
    group = "GROUP BY partition" if is_partitioned(conn, t) else "HAVING count(*) > 0"
    (partitions, data_files, max_per, avg_per, small, cv, data_bytes, pos_deletes, eq_deletes) = query(
        conn,
        f"""
        WITH data AS (SELECT * FROM {files} WHERE content = 0),
        per_partition AS (SELECT count(*) AS n FROM data {group})
        SELECT
          (SELECT count(*) FROM per_partition),
          (SELECT count(*) FROM data),
          (SELECT max(n) FROM per_partition),
          (SELECT avg(n) FROM per_partition),
          (SELECT avg(IF(file_size_in_bytes < {int(small_file_bytes)}, 1.0, 0.0)) FROM data),
          (SELECT stddev_pop(file_size_in_bytes) / nullif(avg(file_size_in_bytes), 0) FROM data),
          (SELECT sum(file_size_in_bytes) FROM data),
          (SELECT count(*) FROM {files} WHERE content = 1),
          (SELECT count(*) FROM {files} WHERE content = 2)
        """,  # noqa: S608
    )[0]
    snapshots = query(conn, f"SELECT count(*) FROM {t.metadata('snapshots')}")[0][0]  # noqa: S608
    manifests = query(conn, f"SELECT count(*) FROM {t.metadata('manifests')}")[0][0]  # noqa: S608
    all_manifests = query(conn, f"SELECT count(DISTINCT path) FROM {t.metadata('all_manifests')}")[0][0]  # noqa: S608
    history_bytes = query(
        conn,
        f"""
        WITH referenced AS (
          SELECT DISTINCT data_file.file_path AS path, data_file.file_size_in_bytes AS bytes
          FROM {t.metadata("all_entries")} WHERE status <> 2
        )
        SELECT coalesce(sum(bytes), 0) FROM referenced
        WHERE path NOT IN (SELECT file_path FROM {files})
        """,  # noqa: S608
    )[0][0]
    return Features(
        partitions=partitions,
        data_files=data_files,
        max_files_per_partition=max_per or 0,
        avg_files_per_partition=float(avg_per or 0),
        small_file_ratio=float(small or 0),
        file_size_cv=float(cv or 0),
        data_bytes=data_bytes or 0,
        position_delete_files=pos_deletes,
        equality_delete_files=eq_deletes,
        snapshots=snapshots,
        manifests=manifests,
        all_manifests=all_manifests,
        history_bytes=history_bytes,
        ref_pinned_bytes=ref_pinned_bytes(conn, t),
    )


def ref_pinned_bytes(conn: trino.dbapi.Connection, table: TableName | str) -> int:
    """Bytes of data files that refs other than `main` reach and the current snapshot does not.

    A ref's data files are the `$path` values its snapshot reads (`FOR VERSION AS OF`), sized from `$all_entries`;
    delete files are not counted.
    """
    t = table_name(table)
    others = [
        r[0]
        for r in query(conn, f"SELECT DISTINCT snapshot_id FROM {t.metadata('refs')} WHERE name <> 'main'")  # noqa: S608
    ]
    if not others:
        return 0
    pinned = " UNION ".join(
        f'SELECT DISTINCT "$path" AS path FROM {t.sql} FOR VERSION AS OF {int(s)}'  # noqa: S608
        for s in others
    )
    return query(
        conn,
        f"""
        WITH pinned AS ({pinned}),
        sizes AS (
          SELECT DISTINCT data_file.file_path AS path, data_file.file_size_in_bytes AS bytes
          FROM {t.metadata("all_entries")}
        )
        SELECT coalesce(sum(sizes.bytes), 0) FROM pinned JOIN sizes ON pinned.path = sizes.path
        WHERE pinned.path NOT IN (SELECT file_path FROM {t.metadata("files")})
        """,  # noqa: S608
    )[0][0]
