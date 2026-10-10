import pytest

from freshness_exporter import config

REQUIRED = {
    "TRINO_HOST": "trino.lakehouse.svc",
    "TRINO_PASSWORD": "not-a-real-password",
    "TRINO_CA_FILE": "/etc/freshness-exporter/ca.crt",
    "FRESHNESS_TABLES": "bronze.orders, bronze.payments",
}


def test_defaults_match_the_lake_ro_contract() -> None:
    cfg = config.load(REQUIRED)

    assert cfg.trino_user == "exporter"
    assert cfg.trino_catalog == "lake_ro"
    assert cfg.trino_port == 8443
    assert cfg.probe_interval_seconds == 60
    assert cfg.tables == ("bronze.orders", "bronze.payments")
    assert cfg.refresh_tables == ()


def test_refresh_tables_are_optional_and_validated() -> None:
    cfg = config.load({**REQUIRED, "REFRESH_TABLES": "gold.fct_orders, gold.dim_customers"})

    assert cfg.refresh_tables == ("gold.fct_orders", "gold.dim_customers")
    with pytest.raises(ValueError, match="REFRESH_TABLES"):
        config.load({**REQUIRED, "REFRESH_TABLES": "gold.Fct_orders"})


def test_a_table_cannot_be_in_both_lists() -> None:
    # Both measures share freshness_probe_success{table}.
    with pytest.raises(ValueError, match="both"):
        config.load({**REQUIRED, "REFRESH_TABLES": "bronze.orders"})


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_variable_fails_fast(missing: str) -> None:
    env = {k: v for k, v in REQUIRED.items() if k != missing}

    with pytest.raises(ValueError, match=missing):
        config.load(env)


@pytest.mark.parametrize(
    "raw",
    [
        "orders",  # no schema
        "bronze.Orders",  # uppercase
        'bronze.orders"; DROP TABLE x; --',  # injection attempt
        "lake.bronze.orders",  # catalog belongs in TRINO_CATALOG
        " , ",  # empty list
    ],
)
def test_invalid_table_lists_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        config.parse_tables(raw)


@pytest.mark.parametrize("raw", ["", "Lake_ro", 'lake_ro"."x', "lake-ro"])
def test_invalid_catalog_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        config.load({**REQUIRED, "TRINO_CATALOG": raw})
