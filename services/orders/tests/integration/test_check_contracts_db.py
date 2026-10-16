"""scripts/check_contracts.py against a real Postgres migrated by Alembic."""

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml
from check_contracts import CONTRACTS_DIR, REPO_ROOT, load_contracts, main
from sqlalchemy import text

from orders.db import create_engine

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def plain_output(monkeypatch):
    """Assert on the local output format, also when the suite itself runs in GitHub Actions."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def execute(url: str, sql: str) -> None:
    async def run() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_contracts_hold_on_the_migrated_schema(app_database, capsys):
    assert main(["--database-url", app_database("contracts_ok")]) == 0
    assert "data contracts OK: 7 tables" in capsys.readouterr().out


def test_schema_change_without_contract_update_fails(app_database, capsys):
    url = app_database("contracts_drop")
    assert main(["--database-url", url]) == 0
    execute(url, "ALTER TABLE payments DROP COLUMN provider_ref")  # what a careless migration would do
    capsys.readouterr()

    assert main(["--database-url", url]) == 1
    assert "ERROR payments: column provider_ref is in the contract but not in the database" in capsys.readouterr().out


ORDERS_VERSION = load_contracts(REPO_ROOT / CONTRACTS_DIR)["orders"].version


def contracts_with_orders_column(tmp_path: Path, name: str, version_bump: int) -> Path:
    """A copy of data/contracts: orders column `customer_id` called `name`, version raised by `version_bump`."""
    contracts = tmp_path / "contracts"
    shutil.copytree(REPO_ROOT / CONTRACTS_DIR, contracts)
    path = contracts / "orders.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["version"] += version_bump
    for column in raw["columns"]:
        if column["name"] == "customer_id":
            column["name"] = name
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return contracts


@pytest.mark.parametrize(
    ("contract_column", "version_bump", "expected_errors"),
    [
        (
            "customer_id",
            0,
            [
                "ERROR orders: column buyer_id is in the database but not in the contract",
                "ERROR orders: column customer_id is in the contract but not in the database (dropped or renamed?)",
            ],
        ),
        (
            "buyer_id",
            0,
            [
                f"ERROR orders: breaking change without a version bump (still {ORDERS_VERSION}):"
                " column customer_id removed or renamed"
            ],
        ),
        ("buyer_id", 1, []),
    ],
    ids=["contract-unchanged", "contract-renamed-same-version", "contract-renamed-version-bumped"],
)
def test_renamed_column(app_database, tmp_path, capsys, request, contract_column, version_bump, expected_errors):
    """A migration renames orders.customer_id to buyer_id; the gate runs as in CI, against the committed contracts.

    Contract unchanged: only schema drift (the contract no longer describes the table). Contract renamed, same
    version: only the missing version bump. Contract renamed and version bumped: the gate passes, although silver
    still reads customer_id; nothing here checks the consumers.
    """
    url = app_database("contracts_" + request.node.callspec.id.replace("-", "_"))
    assert main(["--database-url", url]) == 0
    execute(url, "ALTER TABLE orders RENAME COLUMN customer_id TO buyer_id")
    contracts = contracts_with_orders_column(tmp_path, contract_column, version_bump)
    capsys.readouterr()

    exit_code = main(["--database-url", url, "--contracts-dir", str(contracts), "--base-ref", "HEAD"])
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("ERROR")] == expected_errors
    assert exit_code == (1 if expected_errors else 0)
    if not expected_errors:
        assert "data contracts OK" in out


def test_publication_and_contract_files_must_match(app_database, tmp_path, capsys):
    contracts = tmp_path / "contracts"
    shutil.copytree(REPO_ROOT / CONTRACTS_DIR, contracts)
    (contracts / "heartbeat.yaml").unlink()

    assert main(["--database-url", app_database("contracts_pub"), "--contracts-dir", str(contracts)]) == 1
    assert "heartbeat: published in shop_cdc but has no contract" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("sabotage", "expected"),
    [
        (
            "ALTER PUBLICATION shop_cdc SET TABLE customers, products, orders WHERE (status = 'paid'), order_items,"
            " payments, heartbeat",
            "orders: published with a row filter",
        ),
        (
            "ALTER PUBLICATION shop_cdc SET TABLE customers, products, orders (id, status), order_items, payments,"
            " heartbeat",
            "orders: published with a column list",
        ),
        ("ALTER PUBLICATION shop_cdc SET (publish = 'insert')", "missing: update, delete"),
        ("ALTER TABLE orders REPLICA IDENTITY NOTHING", "orders: REPLICA IDENTITY NOTHING"),
        ("REVOKE SELECT ON payments FROM debezium", "payments: role debezium cannot SELECT it"),
    ],
    ids=["row-filter", "column-list", "insert-only", "replica-identity-nothing", "no-select-grant"],
)
def test_publication_and_capture_problems_fail(app_database, capsys, request, sabotage, expected):
    """Each of these keeps the column contracts intact but silently loses CDC data (or breaks the snapshot)."""
    url = app_database("contracts_" + request.node.callspec.id.replace("-", "_"))
    assert main(["--database-url", url]) == 0
    execute(url, sabotage)  # all doable by the table owner, i.e. by a migration
    capsys.readouterr()

    assert main(["--database-url", url]) == 1
    assert expected in capsys.readouterr().out
