"""Idempotent write statements: applying one a second time changes nothing.

- `insert_if_absent`: a row identified by its key columns is inserted only if no row has that key. Put the partition
  columns in the key too, so Trino reads only that partition to check.
- `add_once`: adds `delta` to a counter row unless the row's ledger already holds the op id, and appends the op id
  in the same statement.

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
