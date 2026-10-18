"""Idempotent write statements: applying one a second time changes nothing.

- `insert_unless_present`: an INSERT … SELECT … WHERE NOT EXISTS of the row's key. An append, so Iceberg does not
  check it against other commits; two attempts of one write must never run at once (`retry.write` sees to that).
- `insert_if_absent`: the same as a MERGE … WHEN NOT MATCHED. Trino checks a MERGE against every concurrent commit to
  the table (its conflict filter is `true`), so concurrent inserts of different rows conflict.
- `update_once`: an UPDATE of one key that adds `delta` and appends the op id to the row's ledger, unless the ledger
  already holds it. Trino scopes an UPDATE's conflict check to the rows its WHERE can match, so updates of keys in
  different partitions do not conflict.
- `add_once`: the same as a MERGE (conflict filter `true`).
- `insert_row`: a plain INSERT, not idempotent. Use it only as the first attempt of a `retry.write` given the table,
  which resolves an ambiguous attempt from the table's snapshots before retrying with an idempotent form.

Put the partition columns in the key, so Trino reads only that partition to check.

Only identifiers matching [a-z0-9_] (tables as catalog.schema.table), integers, dates and short strings of
[A-Za-z0-9_.:-] are accepted, so a statement never carries anything that could change its meaning.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime

Value = int | str | date

_IDENTIFIER = re.compile(r"[a-z0-9_]+")
_STRING = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


def _identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"identifier {name!r} is not [a-z0-9_]+")
    return name


def _table(name: str) -> str:
    parts = name.split(".")
    if len(parts) != 3:
        raise ValueError(f"table {name!r}: name it catalog.schema.table")
    return ".".join(_identifier(part) for part in parts)


def literal(value: Value) -> str:
    if isinstance(value, bool | datetime):
        raise TypeError(f"{type(value).__name__} is not accepted (dates are)")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str) and _STRING.fullmatch(value):
        return f"'{value}'"
    raise ValueError(f"{value!r} is not an accepted literal")


def insert_if_absent(table: str, key: Mapping[str, Value], values: Mapping[str, Value]) -> str:
    if not key:
        raise ValueError("insert_if_absent needs at least one key column")
    columns = [_identifier(c) for c in (*key, *values)]
    if len(set(columns)) != len(columns):
        raise ValueError("a column is both in the key and in the values")
    row = ", ".join(literal(v) for v in (*key.values(), *values.values()))
    match = " AND ".join(f"t.{c} = s.{c}" for c in columns[: len(key)])
    return (
        f"MERGE INTO {_table(table)} t USING (VALUES ({row})) AS s({', '.join(columns)}) ON {match} "  # noqa: S608  # validated literals
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) VALUES ({', '.join(f's.{c}' for c in columns)})"
    )


def add_once(
    table: str, *, key_column: str, key: int, value_column: str, delta: int, ledger_column: str, op_id: str
) -> str:
    k, v, ledger = _identifier(key_column), _identifier(value_column), _identifier(ledger_column)
    return (
        f"MERGE INTO {_table(table)} t USING (VALUES ({literal(key)}, {literal(delta)}, {literal(op_id)})) "
        f"AS s(key, delta, op) ON t.{k} = s.key "
        f"WHEN MATCHED AND NOT contains(t.{ledger}, s.op) "
        f"THEN UPDATE SET {v} = t.{v} + s.delta, {ledger} = t.{ledger} || s.op"
    )


def insert_unless_present(table: str, key: Mapping[str, Value], values: Mapping[str, Value]) -> str:
    if not key:
        raise ValueError("insert_unless_present needs at least one key column")
    columns = [_identifier(c) for c in (*key, *values)]
    if len(set(columns)) != len(columns):
        raise ValueError("a column is both in the key and in the values")
    row = ", ".join(f"{literal(v)} AS {c}" for c, v in zip(columns, (*key.values(), *values.values()), strict=True))
    match = " AND ".join(f"{c} = {literal(v)}" for c, v in key.items())
    target = _table(table)
    return (
        f"INSERT INTO {target} ({', '.join(columns)}) SELECT * FROM (SELECT {row}) "  # noqa: S608  # validated literals
        f"WHERE NOT EXISTS (SELECT 1 FROM {target} WHERE {match})"
    )


def update_once(
    table: str, *, key_column: str, key: int, value_column: str, delta: int, ledger_column: str, op_id: str
) -> str:
    k, v, ledger = _identifier(key_column), _identifier(value_column), _identifier(ledger_column)
    op = literal(op_id)
    return (
        f"UPDATE {_table(table)} SET {v} = {v} + {literal(delta)}, {ledger} = {ledger} || {op} "  # noqa: S608
        f"WHERE {k} = {literal(key)} AND NOT contains({ledger}, {op})"
    )


def insert_row(table: str, values: Mapping[str, Value]) -> str:
    if not values:
        raise ValueError("insert_row needs at least one column")
    columns = [_identifier(c) for c in values]
    row = ", ".join(literal(v) for v in values.values())
    return f"INSERT INTO {_table(table)} ({', '.join(columns)}) VALUES ({row})"  # noqa: S608  # validated literals
