"""Data contracts for the CDC source tables: data/contracts/<table>.yaml vs the schema Alembic builds.

Runs the Alembic migrations on a real Postgres (DATABASE_URL), introspects it and fails when:

1. publication `shop_cdc` is not a plain, explicit table list: it must publish insert, update and delete, only
   tables in `public`, with no row filter or column list (each of those silently drops CDC data);
2. the published tables and the contract files differ (a source table needs both);
3. a published table cannot be captured: no SELECT for role `debezium`, or REPLICA IDENTITY NOTHING;
4. a table's columns (name, type, nullable) or primary key differ from its contract (contract out of date);
5. with --base-ref: compared with the contracts at that git commit, a breaking change comes without a `version`
   bump. Breaking = column dropped or renamed, type not widened, nullable -> NOT NULL, primary key changed,
   contract removed. Adding a column, widening a type and NOT NULL -> nullable are not breaking.

    DATABASE_URL=postgresql://shop_app:shop_app@localhost:25432/shop uv run scripts/check_contracts.py
    uv run scripts/check_contracts.py --base-ref HEAD^1      # what CI runs (base of the PR / previous main)
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
from sqlalchemy.ext.asyncio import AsyncConnection

from orders.db import create_engine
from orders.migrate import upgrade

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = Path("data/contracts")
PUBLICATION = "shop_cdc"
SCHEMA = "public"
CDC_ROLE = "debezium"
REQUIRED_OPERATIONS = ("insert", "update", "delete")


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


@dataclass(frozen=True)
class Snapshot:
    """What the migrated database says about CDC: the publication and every table in `public`."""

    publication_exists: bool
    published: frozenset[tuple[str, str]]  # (schema, table)
    all_tables: bool = False
    missing_operations: tuple[str, ...] = ()
    restricted: dict[str, str] = field(default_factory=dict)  # table -> "row filter" / "column list"
    tables: dict[str, Table] = field(default_factory=dict)
    replica_identity: dict[str, str] = field(default_factory=dict)  # table -> pg_class.relreplident
    cdc_role_exists: bool = True
    cdc_can_select: dict[str, bool] = field(default_factory=dict)


class ContractError(Exception):
    """A contract file or the baseline ref is unusable."""


# --- loading -------------------------------------------------------------------------------------------------


def parse_contract(raw: object, source: str) -> Table:
    if not isinstance(raw, dict):
        raise ContractError(f"{source}: expected a mapping")
    missing = {"table", "version", "primary_key", "columns"} - raw.keys()
    if missing:
        raise ContractError(f"{source}: missing keys {sorted(missing)}")
    if not isinstance(raw["version"], int) or isinstance(raw["version"], bool) or raw["version"] < 1:
        raise ContractError(f"{source}: version must be an integer >= 1")
    if not isinstance(raw["columns"], list) or not raw["columns"]:
        raise ContractError(f"{source}: columns must be a non-empty list")
    columns: dict[str, Column] = {}
    for item in raw["columns"]:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("type"), str)
            and isinstance(item.get("nullable"), bool)
        ):
            raise ContractError(f"{source}: each column needs name (string), type (string), nullable (true/false)")
        if item["name"] in columns:
            raise ContractError(f"{source}: column {item['name']} listed twice")
        columns[item["name"]] = Column(item["name"], item["type"], item["nullable"])
    primary_key = tuple(raw["primary_key"]) if isinstance(raw["primary_key"], list) else ()
    if not primary_key or not set(primary_key) <= columns.keys():
        raise ContractError(f"{source}: primary_key must be a non-empty list of the table's columns")
    if Path(source).stem != raw["table"]:
        raise ContractError(f"{source}: file name must be <table>.yaml (table: {raw['table']})")
    return Table(raw["table"], columns, primary_key, raw["version"], source)


def load_contracts(directory: Path) -> dict[str, Table]:
    tables = {}
    for path in sorted(directory.glob("*.yaml")):
        table = parse_contract(yaml.safe_load(path.read_text()), str(path))
        tables[table.name] = table
    return tables


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)  # noqa: S603,S607


def load_contracts_at(ref: str, directory: Path = CONTRACTS_DIR) -> dict[str, Table]:
    """Contracts as they are at a git commit (empty if the directory did not exist there)."""
    if not ref.strip():
        raise ContractError("--base-ref is empty")
    resolved = _git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    if resolved.returncode != 0:
        raise ContractError(f"base ref {ref!r} is not a commit in this clone (shallow checkout? use fetch-depth: 0)")
    commit = resolved.stdout.strip()
    listing = _git("ls-tree", "--name-only", commit, f"{directory}/")
    if listing.returncode != 0:
        raise ContractError(f"git ls-tree {commit}: {listing.stderr.strip()}")
    tables = {}
    for name in listing.stdout.split():
        if name.endswith(".yaml"):
            shown = _git("show", f"{commit}:{name}")
            if shown.returncode != 0:
                raise ContractError(f"git show {commit}:{name}: {shown.stderr.strip()}")
            table = parse_contract(yaml.safe_load(shown.stdout), f"{ref}:{name}")
            tables[table.name] = table
    return tables


async def _rows(conn: AsyncConnection, sql: str, **params: object) -> list[tuple]:
    return [tuple(row) for row in await conn.execute(text(sql), params)]


async def introspect(database_url: str) -> Snapshot:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as conn:
            publication = await _rows(
                conn,
                "SELECT puballtables, pubinsert, pubupdate, pubdelete FROM pg_publication WHERE pubname = :pub",
                pub=PUBLICATION,
            )
            published = await _rows(
                conn, "SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname = :pub", pub=PUBLICATION
            )
            restricted = await _rows(
                conn,
                """
                SELECT c.relname, pr.prqual IS NOT NULL, pr.prattrs IS NOT NULL
                FROM pg_publication_rel pr
                JOIN pg_publication p ON p.oid = pr.prpubid
                JOIN pg_class c ON c.oid = pr.prrelid
                WHERE p.pubname = :pub AND (pr.prqual IS NOT NULL OR pr.prattrs IS NOT NULL)
                """,
                pub=PUBLICATION,
            )
            columns = await _rows(
                conn,
                """
                SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY c.relname, a.attnum
                """,
                schema=SCHEMA,
            )
            primary_keys = await _rows(
                conn,
                """
                SELECT c.relname, array_agg(a.attname ORDER BY array_position(i.indkey::int2[], a.attnum))
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                WHERE n.nspname = :schema AND i.indisprimary
                GROUP BY c.relname
                """,
                schema=SCHEMA,
            )
            cdc_role_exists = bool(
                (await _rows(conn, "SELECT count(*) FROM pg_roles WHERE rolname = :role", role=CDC_ROLE))[0][0]
            )
            # has_table_privilege() errors on an unknown role, so only ask when the role exists.
            can_select = f"has_table_privilege('{CDC_ROLE}', c.oid, 'SELECT')" if cdc_role_exists else "false"
            capture = await _rows(
                conn,
                f"""
                SELECT c.relname, c.relreplident::text, {can_select}
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')
                """,  # noqa: S608 - CDC_ROLE is a module constant
                schema=SCHEMA,
            )
    finally:
        await engine.dispose()

    pks = {name: tuple(cols) for name, cols in primary_keys}
    by_table: dict[str, dict[str, Column]] = {}
    for table, column, type_, nullable in columns:
        by_table.setdefault(table, {})[column] = Column(column, type_, nullable)
    all_tables, *operations = publication[0] if publication else (False, True, True, True)
    return Snapshot(
        publication_exists=bool(publication),
        published=frozenset(published),
        all_tables=all_tables,
        missing_operations=tuple(op for op, on in zip(REQUIRED_OPERATIONS, operations, strict=True) if not on),
        restricted={name: "row filter" if has_filter else "column list" for name, has_filter, _ in restricted},
        tables={name: Table(name, cols, pks.get(name, ())) for name, cols in by_table.items()},
        replica_identity={name: identity for name, identity, _ in capture},
        cdc_role_exists=cdc_role_exists,
        cdc_can_select={name: can_select for name, _, can_select in capture},
    )


# --- rules ---------------------------------------------------------------------------------------------------

_INT_RANK = {"smallint": 1, "integer": 2, "bigint": 3}
_NUMERIC = re.compile(r"numeric\((\d+),(\d+)\)")
_VARCHAR = re.compile(r"character varying\((\d+)\)")
_UNBOUNDED_TEXT = {"text", "character varying"}  # same storage and values in Postgres


def is_widening(old: str, new: str) -> bool:
    """True if every value of `old` fits `new` unchanged *and* consumers can follow the change.

    Unknown pairs count as breaking (conservative). Decimals may only gain precision at the same scale: Iceberg
    allows decimal(P,S) -> decimal(P',S) with P' > P only, and Debezium encodes unconstrained `numeric` as a
    different (variable-scale) structure.
    """
    if old == new or (old in _UNBOUNDED_TEXT and new in _UNBOUNDED_TEXT):
        return True
    if old in _INT_RANK and new in _INT_RANK:
        return _INT_RANK[new] >= _INT_RANK[old]
    old_numeric, new_numeric = _NUMERIC.fullmatch(old), _NUMERIC.fullmatch(new)
    if old_numeric and new_numeric:
        (p1, s1), (p2, s2) = map(int, old_numeric.groups()), map(int, new_numeric.groups())
        return s2 == s1 and p2 >= p1
    old_varchar, new_varchar = _VARCHAR.fullmatch(old), _VARCHAR.fullmatch(new)
    if old_varchar and new in _UNBOUNDED_TEXT:
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
            changes.append(f"column {name}: type {old.type} -> {new.type} is not a safe widening")
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


def publication_errors(db: Snapshot) -> list[tuple[str, str]]:
    if not db.publication_exists:
        return [(PUBLICATION, f"publication {PUBLICATION} does not exist")]
    errors = []
    if db.all_tables:
        errors.append((PUBLICATION, "must list its tables explicitly, never FOR ALL TABLES"))
    if db.missing_operations:
        missing = ", ".join(db.missing_operations)
        errors.append((PUBLICATION, f"must publish {', '.join(REQUIRED_OPERATIONS)}; missing: {missing}"))
    for schema, table in sorted(db.published):
        if schema != SCHEMA:
            errors.append((table, f"{schema}.{table} is published, but source tables must be in {SCHEMA}"))
    for table, kind in sorted(db.restricted.items()):
        errors.append((table, f"published with a {kind}: CDC would silently drop data"))
    return errors


def check(db: Snapshot, contracts: dict[str, Table], base: dict[str, Table] | None = None) -> list[tuple[str, str]]:
    """All problems as (table, message); empty means the contracts hold."""
    errors = publication_errors(db)
    published = {table for schema, table in db.published if schema == SCHEMA}
    for name in sorted(published - contracts.keys()):
        errors.append((name, f"published in {PUBLICATION} but has no contract (add data/contracts/{name}.yaml)"))
    for name in sorted(contracts.keys() - published):
        errors.append((name, f"has a contract but is not published in {PUBLICATION}"))
    if not db.cdc_role_exists:
        errors.append((PUBLICATION, f"role {CDC_ROLE} does not exist"))
    for name in sorted(contracts.keys()):
        if name not in db.tables:
            errors.append((name, "has a contract but the table does not exist"))
            continue
        errors.extend((name, problem) for problem in schema_drift(contracts[name], db.tables[name]))
        if db.replica_identity.get(name) == "n":
            errors.append((name, "REPLICA IDENTITY NOTHING: updates and deletes cannot be captured"))
        if db.cdc_role_exists and not db.cdc_can_select.get(name, False):
            errors.append((name, f"role {CDC_ROLE} cannot SELECT it: the Debezium snapshot would fail"))
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
    parser.add_argument("--base-ref", help="git commit whose contracts are the baseline for breaking-change checks")
    parser.add_argument("--contracts-dir", type=Path, default=REPO_ROOT / CONTRACTS_DIR)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("set DATABASE_URL or --database-url")

    try:
        contracts = load_contracts(args.contracts_dir)
        base = load_contracts_at(args.base_ref) if args.base_ref is not None else None
    except ContractError as exc:
        print(f"ERROR {exc}")
        return 1

    upgrade(args.database_url)  # the schema under test is exactly what the migrations build
    errors = check(asyncio.run(introspect(args.database_url)), contracts, base)
    report(errors)
    baseline = f", baseline {args.base_ref}" if args.base_ref else ""
    if errors:
        print(f"{len(errors)} data contract problem(s) in {len(contracts)} contracts{baseline}")
        return 1
    print(f"data contracts OK: {len(contracts)} tables match {PUBLICATION} and the migrated schema{baseline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
