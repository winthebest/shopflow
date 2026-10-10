"""Rules of scripts/check_contracts.py (no database)."""

from dataclasses import replace
from pathlib import Path

import pytest
from check_contracts import (
    CONTRACTS_DIR,
    REPO_ROOT,
    Column,
    ContractError,
    Snapshot,
    Table,
    breaking_changes,
    check,
    compatibility_errors,
    is_widening,
    load_contracts,
    load_contracts_at,
    parse_contract,
    report,
)

ORDERS = Table(
    "orders",
    {
        "id": Column("id", "bigint", False),
        "status": Column("status", "text", False),
        "total": Column("total", "numeric(12,2)", False),
        "note": Column("note", "character varying(50)", True),
    },
    ("id",),
    version=1,
)


def without(table: Table, column: str) -> Table:
    return replace(table, columns={k: v for k, v in table.columns.items() if k != column})


def with_column(table: Table, column: Column) -> Table:
    return replace(table, columns={**table.columns, column.name: column})


def healthy(**overrides) -> Snapshot:
    """A database where `orders` is published, capturable and matches ORDERS."""
    values = {
        "publication_exists": True,
        "published": frozenset({("public", "orders")}),
        "tables": {"orders": replace(ORDERS, version=0)},
        "replica_identity": {"orders": "d"},
        "cdc_can_select": {"orders": True},
    }
    return Snapshot(**(values | overrides))


@pytest.mark.parametrize(
    ("old", "new", "widening"),
    [
        ("integer", "bigint", True),
        ("bigint", "integer", False),
        ("smallint", "integer", True),
        ("numeric(12,2)", "numeric(14,2)", True),
        ("numeric(12,2)", "numeric(13,3)", False),  # scale change: Iceberg cannot follow
        ("numeric(12,2)", "numeric(10,2)", False),
        ("numeric(12,2)", "numeric(12,1)", False),
        ("numeric(12,2)", "numeric", False),  # Debezium switches to a variable-scale structure
        ("character varying(50)", "character varying(80)", True),
        ("character varying(50)", "character varying(20)", False),
        ("character varying(50)", "text", True),
        ("character varying", "text", True),
        ("text", "character varying", True),
        ("text", "character varying(50)", False),
        ("timestamp without time zone", "timestamp with time zone", False),  # meaning changes: treat as breaking
        ("text", "bigint", False),
    ],
)
def test_is_widening(old, new, widening):
    assert is_widening(old, new) is widening


def test_non_breaking_changes():
    head = with_column(ORDERS, Column("channel", "text", True))  # added column
    head = with_column(head, Column("note", "text", True))  # varchar(50) -> text: widening
    head = with_column(head, Column("status", "text", True))  # NOT NULL -> nullable
    head = with_column(head, Column("total", "numeric(14,2)", False))  # more precision, same scale
    assert breaking_changes(ORDERS, head) == []


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (without(ORDERS, "total"), "column total removed or renamed"),
        (
            with_column(ORDERS, Column("total", "numeric(10,2)", False)),
            "column total: type numeric(12,2) -> numeric(10,2)",
        ),
        (
            with_column(ORDERS, Column("total", "numeric(13,3)", False)),
            "column total: type numeric(12,2) -> numeric(13,3)",
        ),
        (with_column(ORDERS, Column("note", "character varying(50)", False)), "column note: nullable -> NOT NULL"),
        (replace(ORDERS, primary_key=("id", "status")), "primary key ['id'] -> ['id', 'status']"),
    ],
    ids=["drop", "narrow", "scale-change", "not-null", "primary-key"],
)
def test_breaking_change_needs_a_version_bump(head, expected):
    errors = compatibility_errors({"orders": ORDERS}, {"orders": head})
    assert len(errors) == 1
    assert "breaking change without a version bump" in errors[0][1]
    assert expected in errors[0][1]

    assert compatibility_errors({"orders": ORDERS}, {"orders": replace(head, version=2)}) == []


def test_version_cannot_go_down_and_tables_cannot_disappear():
    assert "version went down" in compatibility_errors({"orders": replace(ORDERS, version=2)}, {"orders": ORDERS})[0][1]
    assert "contract removed" in compatibility_errors({"orders": ORDERS}, {})[0][1]
    assert compatibility_errors({}, {"orders": ORDERS}) == []  # a new source table is fine


def test_healthy_database_passes():
    assert check(healthy(), {"orders": ORDERS}) == []


@pytest.mark.parametrize(
    ("db", "expected"),
    [
        (healthy(publication_exists=False), "publication shop_cdc does not exist"),
        (healthy(all_tables=True), "never FOR ALL TABLES"),
        (healthy(missing_operations=("delete",)), "missing: delete"),
        (healthy(restricted={"orders": "row filter"}), "published with a row filter"),
        (healthy(restricted={"orders": "column list"}), "published with a column list"),
        (
            healthy(published=frozenset({("public", "orders"), ("meta", "cdc_epochs")})),
            "meta.cdc_epochs is published, but source tables must be in public",
        ),
        (healthy(published=frozenset({("public", "orders"), ("public", "heartbeat")})), "has no contract"),
        (healthy(published=frozenset()), "has a contract but is not published"),
        (healthy(replica_identity={"orders": "n"}), "REPLICA IDENTITY NOTHING"),
        (healthy(cdc_can_select={"orders": False}), "role debezium cannot SELECT it"),
        (healthy(cdc_role_exists=False), "role debezium does not exist"),
        (healthy(tables={"orders": without(ORDERS, "note")}), "column note is in the contract but not in the database"),
        (healthy(tables={}), "the table does not exist"),
    ],
    ids=[
        "no-publication",
        "all-tables",
        "missing-operation",
        "row-filter",
        "column-list",
        "non-public-table",
        "published-without-contract",
        "contract-not-published",
        "replica-identity-nothing",
        "no-select-grant",
        "no-cdc-role",
        "schema-drift",
        "missing-table",
    ],
)
def test_check_fails(db, expected):
    errors = check(db, {"orders": ORDERS})
    assert any(expected in message for _, message in errors), errors


def test_repository_contracts_cover_the_source_tables():
    contracts = load_contracts(REPO_ROOT / CONTRACTS_DIR)
    assert set(contracts) == {"customers", "products", "orders", "order_items", "payments", "heartbeat", "shipments"}
    assert all(table.version >= 1 and table.primary_key for table in contracts.values())


def test_contracts_at_a_git_commit():
    assert set(load_contracts_at("HEAD")) == set(load_contracts(REPO_ROOT / CONTRACTS_DIR))


@pytest.mark.parametrize("ref", ["", "  ", "no-such-ref-xyz", "0000000000000000000000000000000000000000", "--help"])
def test_unusable_base_ref_is_a_clear_error(ref):
    with pytest.raises(ContractError):
        load_contracts_at(ref)


COLUMN = {"name": "id", "type": "bigint", "nullable": False}


@pytest.mark.parametrize(
    "raw",
    [
        {"table": "orders", "version": 0, "primary_key": ["id"], "columns": [COLUMN]},
        {"table": "orders", "version": True, "primary_key": ["id"], "columns": [COLUMN]},
        {"table": "orders", "version": 1, "primary_key": ["nope"], "columns": [COLUMN]},
        {"table": "orders", "version": 1, "primary_key": [], "columns": [COLUMN]},
        {"table": "orders", "version": 1, "primary_key": ["id"], "columns": [COLUMN | {"nullable": "false"}]},
        {"table": "orders", "version": 1, "primary_key": ["id"], "columns": [{"name": "id"}]},
        {"table": "orders", "version": 1, "primary_key": ["id"], "columns": [COLUMN, COLUMN]},
        {"table": "other", "version": 1, "primary_key": ["id"], "columns": [COLUMN]},
        {"table": "orders", "version": 1, "columns": [COLUMN]},
    ],
    ids=[
        "version-0",
        "version-bool",
        "unknown-pk",
        "empty-pk",
        "nullable-string",
        "column-missing-keys",
        "duplicate-column",
        "file-name-mismatch",
        "missing-key",
    ],
)
def test_malformed_contract_is_rejected(raw):
    with pytest.raises(ContractError):
        parse_contract(raw, str(Path("data/contracts/orders.yaml")))


@pytest.mark.parametrize(
    ("in_actions", "expected"),
    [
        (False, "ERROR orders: column note dropped\n"),
        (True, "::error file=data/contracts/orders.yaml,title=data contract orders::column note dropped\n"),
    ],
)
def test_report_format(monkeypatch, capsys, in_actions, expected):
    if in_actions:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    report([("orders", "column note dropped")])
    assert capsys.readouterr().out == expected
