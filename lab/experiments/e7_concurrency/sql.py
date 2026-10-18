"""The experiment's SQL: tables, seed, the baseline writes, maintenance and the read-back.

Every statement is built from constants, integers and ids the harness generated (`writer:seq`, checked by `_op_id`),
never from outside input; the strings are kept literal so the SQL log shows exactly what ran.
"""

import re
from datetime import date

CATALOG = "lab_exp"
SCHEMA = "lab_e7"
EVENTS = f"{CATALOG}.{SCHEMA}.events"
COUNTERS = f"{CATALOG}.{SCHEMA}.counters"
BUCKETS = 8

_WRITER = re.compile(r"[a-z0-9-]+")
_OP_ID = re.compile(r"[a-z0-9-]+:[0-9]+")


def _writer(writer: str) -> str:
    if not _WRITER.fullmatch(writer):
        raise ValueError(f"writer id {writer!r} is not [a-z0-9-]+")
    return writer


def _op_id(oid: str) -> str:
    if not _OP_ID.fullmatch(oid):
        raise ValueError(f"op id {oid!r} is not writer:seq")
    return oid


def _int(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{value!r} is not an integer")
    return value


RETIRED = "_retired_"
_SUFFIX = re.compile(r"[a-z0-9_]{1,60}")


def recreate_tables(*, retire: str, ledger: bool = True) -> list[str]:
    """Statements that set the previous run's tables aside and create both afresh, so a run inherits no files,
    snapshots or ledgers. The old tables are renamed to <table>_retired_<retire>, not dropped: a drop makes Polaris
    purge every data file in the background, and between runs a minute apart those purges exhausted its S3
    connection pool and stalled the next run's commits (2026-10-18). They stay until the lab stack is reset, which
    removes its volumes without a purge."""
    if not _SUFFIX.fullmatch(retire):
        raise ValueError(f"retire suffix {retire!r} is not [a-z0-9_]+")
    applied = ", applied array(varchar) NOT NULL" if ledger else ""
    return [
        f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}",
        f"ALTER TABLE IF EXISTS {EVENTS} RENAME TO {EVENTS}{RETIRED}{retire}",
        f"ALTER TABLE IF EXISTS {COUNTERS} RENAME TO {COUNTERS}{RETIRED}{retire}",
        f"CREATE TABLE {EVENTS} (writer_id varchar NOT NULL, seq bigint NOT NULL, key bigint NOT NULL, "
        f"delta bigint NOT NULL, event_day date NOT NULL) "
        f"WITH (format_version = 2, partitioning = ARRAY['bucket(key, {BUCKETS})', 'event_day'])",
        f"CREATE TABLE {COUNTERS} (key bigint NOT NULL, value bigint NOT NULL{applied}) "
        f"WITH (format_version = 2, partitioning = ARRAY['bucket(key, {BUCKETS})'])",
    ]


def seed_counters(keys: range, *, ledger: bool = True) -> str:
    """One row per key, value 0 (and an empty ledger), in one commit."""
    if keys.step != 1 or keys.start < 0:
        raise ValueError("seed keys must be a contiguous range from 0 or more")
    applied = ", CAST(ARRAY[] AS array(varchar))" if ledger else ""
    return (
        f"INSERT INTO {COUNTERS} SELECT k, 0{applied} "  # noqa: S608  # constants and integers only
        f"FROM UNNEST(sequence({_int(keys.start)}, {_int(keys.stop - 1)})) AS t(k)"
    )


def insert_event(writer: str, seq: int, key: int, delta: int, day: date) -> str:
    """The baseline event write: a plain INSERT, one op per statement."""
    return (
        f"INSERT INTO {EVENTS} VALUES ('{_writer(writer)}', {_int(seq)}, {_int(key)}, {_int(delta)}, "  # noqa: S608
        f"DATE '{day.isoformat()}')"
    )


def add_to_counter(oid: str, key: int, delta: int, *, ledger: bool = True) -> str:
    """The baseline counter write: read-modify-write in one MERGE. With the ledger the op id is appended to
    `applied` in the same statement."""
    applied = f", applied = t.applied || '{_op_id(oid)}'" if ledger else ""
    return (
        f"MERGE INTO {COUNTERS} t USING (VALUES ({_int(key)}, {_int(delta)})) AS s(key, delta) "
        f"ON t.key = s.key WHEN MATCHED THEN UPDATE SET value = t.value + s.delta{applied}"
    )


def optimize(table: str, *, before_day: date | None = None) -> str:
    """OPTIMIZE on the whole table, or (events only) on the day partitions before `before_day`."""
    if table not in (EVENTS, COUNTERS):
        raise ValueError(f"not an experiment table: {table}")
    if before_day is None:
        return f"ALTER TABLE {table} EXECUTE optimize"
    if table != EVENTS:
        raise ValueError("only events has a day partition")
    return f"ALTER TABLE {table} EXECUTE optimize WHERE event_day < DATE '{before_day.isoformat()}'"


READ_EVENTS = f"SELECT writer_id, seq, key, delta FROM {EVENTS}"  # noqa: S608
READ_COUNTERS = f"SELECT key, value, applied FROM {COUNTERS}"  # noqa: S608
READ_COUNTERS_NO_LEDGER = f"SELECT key, value FROM {COUNTERS}"  # noqa: S608


def snapshots(table: str) -> str:
    """The table's snapshots with their summaries (bytes added, the operation, and Trino's query id if it is there)."""
    if table not in (EVENTS, COUNTERS):
        raise ValueError(f"not an experiment table: {table}")
    catalog, schema, name = table.split(".")
    return (
        f'SELECT snapshot_id, operation, summary FROM {catalog}.{schema}."{name}$snapshots" '  # noqa: S608
        "ORDER BY committed_at"
    )
