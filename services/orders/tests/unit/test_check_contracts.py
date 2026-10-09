"""Rules of scripts/check_contracts.py (no database)."""

from dataclasses import replace
from pathlib import Path

import pytest
from check_contracts import (
    CONTRACTS_DIR,
    REPO_ROOT,
    Column,
    ContractError,
    Table,
    breaking_changes,
    check,
    compatibility_errors,
    is_widening,
    load_contracts,
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


@pytest.mark.parametrize(
    ("old", "new", "widening"),
    [
        ("integer", "bigint", True),
        ("bigint", "integer", False),
        ("smallint", "integer", True),
        ("numeric(12,2)", "numeric(14,2)", True),
        ("numeric(12,2)", "numeric(13,3)", True),
        ("numeric(12,2)", "numeric(10,2)", False),
        ("numeric(12,2)", "numeric(12,1)", False),
        ("numeric(12,2)", "numeric", True),
        ("character varying(50)", "character varying(80)", True),
        ("character varying(50)", "character varying(20)", False),
        ("character varying(50)", "text", True),
        ("text", "character varying(50)", False),
        ("timestamp without time zone", "timestamp with time zone", False),  # meaning changes: treat as breaking
        ("text", "bigint", False),
    ],
)
def test_is_widening(old, new, widening):
    assert is_widening(old, new) is widening


def test_non_breaking_changes():
    head = with_column(ORDERS, Column("channel", "text", True))  # added column
    head = with_column(head, Column("id", "bigint", False))
    head = with_column(head, Column("note", "text", True))  # varchar(50) -> text: widening
    head = with_column(head, Column("status", "text", True))  # NOT NULL -> nullable
    assert breaking_changes(ORDERS, head) == []


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (without(ORDERS, "total"), "column total removed or renamed"),
        (
            with_column(ORDERS, Column("total", "numeric(10,2)", False)),
            "column total: type numeric(12,2) -> numeric(10,2)",
        ),
        (with_column(ORDERS, Column("note", "character varying(50)", False)), "column note: nullable -> NOT NULL"),
        (replace(ORDERS, primary_key=("id", "status")), "primary key ['id'] -> ['id', 'status']"),
    ],
    ids=["drop", "narrow", "not-null", "primary-key"],
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


def test_check_compares_publication_contracts_and_schema():
    actual = {"orders": ORDERS, "heartbeat": Table("heartbeat", {"id": Column("id", "smallint", False)}, ("id",))}
    contracts = {"orders": ORDERS}
    errors = dict(check({"orders", "heartbeat"}, actual, contracts))
    assert "has no contract" in errors["heartbeat"]

    errors = dict(check({"orders"}, {"orders": without(ORDERS, "note")}, contracts))
    assert errors == {"orders": "column note is in the contract but not in the database (dropped or renamed?)"}

    assert check({"orders"}, actual, contracts) == []


def test_repository_contracts_cover_the_six_source_tables():
    contracts = load_contracts(REPO_ROOT / CONTRACTS_DIR)
    assert set(contracts) == {"customers", "products", "orders", "order_items", "payments", "heartbeat"}
    assert all(table.version >= 1 and table.primary_key for table in contracts.values())


@pytest.mark.parametrize(
    "raw",
    [
        {
            "table": "orders",
            "version": 0,
            "primary_key": ["id"],
            "columns": [{"name": "id", "type": "bigint", "nullable": False}],
        },
        {
            "table": "orders",
            "version": 1,
            "primary_key": ["nope"],
            "columns": [{"name": "id", "type": "bigint", "nullable": False}],
        },
        {
            "table": "other",
            "version": 1,
            "primary_key": ["id"],
            "columns": [{"name": "id", "type": "bigint", "nullable": False}],
        },
        {"table": "orders", "version": 1, "columns": []},
    ],
    ids=["version-0", "unknown-pk", "file-name-mismatch", "missing-key"],
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
