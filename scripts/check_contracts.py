"""Data contracts for the CDC source tables: data/contracts/<table>.yaml vs the schema Alembic builds.

Runs the Alembic migrations on a real Postgres (DATABASE_URL), introspects it and fails when:

1. the tables in publication `shop_cdc` and the contract files differ (a source table needs both);
2. a table's columns (name, type, nullable) or primary key differ from its contract (contract out of date);
3. with --base-ref: compared with the contracts at that git ref, a breaking change comes without a `version`
   bump. Breaking = column dropped or renamed, type narrowed or changed, nullable -> NOT NULL, primary key
   changed, table removed. Adding a column, widening a type and NOT NULL -> nullable are not breaking.

    DATABASE_URL=postgresql://shop_app:shop_app@localhost:25432/shop uv run scripts/check_contracts.py
    uv run scripts/check_contracts.py --base-ref origin/main      # what CI runs on a pull request
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import text

from orders.db import create_engine
from orders.migrate import upgrade

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = Path("data/contracts")
PUBLICATION = "shop_cdc"
SCHEMA = "public"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class Table:
    name: str
    columns: dict[str, Column]
    primary_key: tuple[str, ...]
    version: int = 0  # contracts only; the database has no version
    source: str = field(default="", compare=False)  # file the contract came from, for error messages


class ContractError(Exception):
    """A contract file is malformed."""


# --- loading -------------------------------------------------------------------------------------------------


def parse_contract(raw: object, source: str) -> Table:
    if not isinstance(raw, dict):
        raise ContractError(f"{source}: expected a mapping")
    missing = {"table", "version", "primary_key", "columns"} - raw.keys()
    if missing:
        raise ContractError(f"{source}: missing keys {sorted(missing)}")
    if not isinstance(raw["version"], int) or raw["version"] < 1:
        raise ContractError(f"{source}: version must be an integer >= 1")
    columns: dict[str, Column] = {}
    for item in raw["columns"]:
        column = Column(name=str(item["name"]), type=str(item["type"]), nullable=bool(item["nullable"]))
        if column.name in columns:
            raise ContractError(f"{source}: column {column.name} listed twice")
        columns[column.name] = column
    primary_key = tuple(raw["primary_key"])
    if not set(primary_key) <= columns.keys():
        raise ContractError(f"{source}: primary_key {list(primary_key)} names unknown columns")
    if Path(source).stem != raw["table"]:
        raise ContractError(f"{source}: file name must be <table>.yaml (table: {raw['table']})")
    return Table(raw["table"], columns, primary_key, raw["version"], source)


def load_contracts(directory: Path) -> dict[str, Table]:
    tables = {}
    for path in sorted(directory.glob("*.yaml")):
        table = parse_contract(yaml.safe_load(path.read_text()), str(path))
        tables[table.name] = table
    return tables


def load_contracts_at(ref: str, directory: Path = CONTRACTS_DIR) -> dict[str, Table]:
    """Contracts as they are at a git ref (empty if the directory did not exist there)."""
    names = subprocess.run(  # noqa: S603 - fixed git command, ref from CI
        ["git", "ls-tree", "--name-only", ref, f"{directory}/"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    tables = {}
    for name in names:
        if not name.endswith(".yaml"):
            continue
        content = subprocess.run(  # noqa: S603
            ["git", "show", f"{ref}:{name}"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        table = parse_contract(yaml.safe_load(content), f"{ref}:{name}")
        tables[table.name] = table
    return tables


async def introspect(database_url: str) -> tuple[set[str], dict[str, Table]]:
    """(tables in the publication, schema of every table in `public`)."""
    engine = create_engine(database_url)
    try:
        async with engine.connect() as conn:
            published = set(
                await conn.scalars(
                    text("SELECT tablename FROM pg_publication_tables WHERE pubname = :pub AND schemaname = :schema"),
                    {"pub": PUBLICATION, "schema": SCHEMA},
                )
            )
            column_rows = await conn.execute(
                text(
                    """
                    SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
                    ORDER BY c.relname, a.attnum
                    """
                ),
                {"schema": SCHEMA},
            )
            pk_rows = await conn.execute(
                text(
                    """
                    SELECT c.relname, array_agg(a.attname ORDER BY array_position(i.indkey::int2[], a.attnum))
                    FROM pg_index i
                    JOIN pg_class c ON c.oid = i.indrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                    WHERE n.nspname = :schema AND i.indisprimary
                    GROUP BY c.relname
                    """
                ),
                {"schema": SCHEMA},
            )
            primary_keys = {name: tuple(columns) for name, columns in pk_rows}
            columns_by_table: dict[str, dict[str, Column]] = {}
            for table, column, type_, nullable in column_rows:
                columns_by_table.setdefault(table, {})[column] = Column(column, type_, nullable)
    finally:
        await engine.dispose()
    tables = {name: Table(name, columns, primary_keys.get(name, ())) for name, columns in columns_by_table.items()}
    return published, tables


# --- rules ---------------------------------------------------------------------------------------------------

_INT_RANK = {"smallint": 1, "integer": 2, "bigint": 3}
_NUMERIC = re.compile(r"numeric\((\d+),(\d+)\)")
_VARCHAR = re.compile(r"character varying\((\d+)\)")


def is_widening(old: str, new: str) -> bool:
    """True if every value of type `old` fits `new` unchanged. Unknown pairs count as narrowing (conservative)."""
    if old == new:
        return True
    if old in _INT_RANK and new in _INT_RANK:
        return _INT_RANK[new] >= _INT_RANK[old]
    old_numeric, new_numeric = _NUMERIC.fullmatch(old), _NUMERIC.fullmatch(new)
    if old_numeric and new == "numeric":
        return True
    if old_numeric and new_numeric:
        (p1, s1), (p2, s2) = map(int, old_numeric.groups()), map(int, new_numeric.groups())
        return p2 - s2 >= p1 - s1 and s2 >= s1
    old_varchar, new_varchar = _VARCHAR.fullmatch(old), _VARCHAR.fullmatch(new)
    if old_varchar and new in ("text", "character varying"):
        return True
    if old_varchar and new_varchar:
        return int(new_varchar.group(1)) >= int(old_varchar.group(1))
    return False


def schema_drift(contract: Table, actual: Table) -> list[str]:
    """Differences between a contract and the database table (the contract must describe the schema exactly)."""
    problems = []
    for name in contract.columns.keys() - actual.columns.keys():
        problems.append(f"column {name} is in the contract but not in the database (dropped or renamed?)")
    for name in actual.columns.keys() - contract.columns.keys():
        problems.append(f"column {name} is in the database but not in the contract")
    for name in contract.columns.keys() & actual.columns.keys():
        want, have = contract.columns[name], actual.columns[name]
        if want.type != have.type:
            problems.append(f"column {name}: contract type {want.type}, database type {have.type}")
        if want.nullable != have.nullable:
            problems.append(f"column {name}: contract nullable={want.nullable}, database nullable={have.nullable}")
    if contract.primary_key != actual.primary_key:
        problems.append(f"primary key: contract {list(contract.primary_key)}, database {list(actual.primary_key)}")
    return sorted(problems)


def breaking_changes(base: Table, head: Table) -> list[str]:
    changes = []
    for name in base.columns.keys() - head.columns.keys():
        changes.append(f"column {name} removed or renamed")
    for name in base.columns.keys() & head.columns.keys():
        old, new = base.columns[name], head.columns[name]
        if not is_widening(old.type, new.type):
            changes.append(f"column {name}: type {old.type} -> {new.type} is not a widening")
        if old.nullable and not new.nullable:
            changes.append(f"column {name}: nullable -> NOT NULL")
    if base.primary_key != head.primary_key:
        changes.append(f"primary key {list(base.primary_key)} -> {list(head.primary_key)}")
    return sorted(changes)


def compatibility_errors(base: dict[str, Table], head: dict[str, Table]) -> list[tuple[str, str]]:
    errors = []
    for name in sorted(base.keys() - head.keys()):
        errors.append((name, "contract removed: dropping a source table breaks consumers; coordinate with sf-data"))
    for name in sorted(base.keys() & head.keys()):
        old, new = base[name], head[name]
        if new.version < old.version:
            errors.append((name, f"version went down ({old.version} -> {new.version})"))
            continue
        changes = breaking_changes(old, new)
        if changes and new.version == old.version:
            listed = "; ".join(changes)
            errors.append((name, f"breaking change without a version bump (still {old.version}): {listed}"))
    return errors


def check(
    published: set[str], actual: dict[str, Table], contracts: dict[str, Table], base: dict[str, Table] | None = None
) -> list[tuple[str, str]]:
    """All problems as (table, message); empty means the contracts hold."""
    errors = []
    for name in sorted(published - contracts.keys()):
        errors.append((name, f"published in {PUBLICATION} but has no contract (add data/contracts/{name}.yaml)"))
    for name in sorted(contracts.keys() - published):
        errors.append((name, f"has a contract but is not published in {PUBLICATION}"))
    for name in sorted(contracts.keys()):
        if name not in actual:
            errors.append((name, "has a contract but the table does not exist"))
            continue
        errors.extend((name, problem) for problem in schema_drift(contracts[name], actual[name]))
    if base is not None:
        errors.extend(compatibility_errors(base, contracts))
    return errors


# --- CLI -----------------------------------------------------------------------------------------------------


def report(errors: list[tuple[str, str]]) -> None:
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for table, message in errors:
        if in_actions:
            print(f"::error file={CONTRACTS_DIR / table}.yaml,title=data contract {table}::{message}")
        else:
            print(f"ERROR {table}: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), help="default: $DATABASE_URL")
    parser.add_argument("--base-ref", help="git ref whose contracts are the baseline for breaking-change checks")
    parser.add_argument("--contracts-dir", type=Path, default=REPO_ROOT / CONTRACTS_DIR)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("set DATABASE_URL or --database-url")

    try:
        contracts = load_contracts(args.contracts_dir)
        base = load_contracts_at(args.base_ref) if args.base_ref else None
    except ContractError as exc:
        print(f"ERROR {exc}")
        return 1

    upgrade(args.database_url)  # the schema under test is exactly what the migrations build
    published, actual = asyncio.run(introspect(args.database_url))
    errors = check(published, actual, contracts, base)
    report(errors)
    baseline = f", baseline {args.base_ref}" if args.base_ref else ""
    if errors:
        print(f"{len(errors)} data contract problem(s) in {len(contracts)} contracts{baseline}")
        return 1
    print(f"data contracts OK: {len(contracts)} tables match {PUBLICATION} and the migrated schema{baseline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
