import pytest
from prometheus_client import CollectorRegistry

from freshness_exporter.probe import (
    FreshnessMetrics,
    has_rows_query,
    latest_data_commit_query,
    max_source_ts_query,
    newest_data_commit,
    probe_once,
)

NOW = 1_800_000_000.0


def sample(registry: CollectorRegistry, name: str, table: str) -> float | None:
    return registry.get_sample_value(name, {"table": table})


@pytest.fixture
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry) -> FreshnessMetrics:
    return FreshnessMetrics(registry)


def test_source_query_quotes_catalog_schema_and_table() -> None:
    assert max_source_ts_query("lake_ro", "bronze.orders") == (
        'SELECT max("_source_ts_ms") FROM "lake_ro"."bronze"."orders"'
    )


def test_commit_query_reads_snapshots_without_replace_operations() -> None:
    assert latest_data_commit_query("lake_ro", "gold.fct_orders") == (
        'SELECT to_unixtime(max(committed_at)) FROM "lake_ro"."gold"."fct_orders$snapshots" '
        "WHERE operation <> 'replace'"
    )


def test_has_rows_query_reads_at_most_one_row() -> None:
    assert has_rows_query("lake_ro", "bronze.orders") == (
        'SELECT count(*) FROM (SELECT 1 FROM "lake_ro"."bronze"."orders" LIMIT 1)'
    )


ROWS_FROM = {"gold.fct_orders": "bronze.orders", "gold.dim_customers": "bronze.customers"}


def commit_and_rows(non_empty: set[str]):
    """A commit 60 s ago for every table, and has_rows for the given tables, recording which tables were asked."""
    asked: list[str] = []

    def has_rows(table: str) -> bool:
        asked.append(table)
        return table in non_empty

    return (lambda _: NOW - 60), has_rows, asked


@pytest.mark.parametrize(
    ("table", "non_empty", "fresh"),
    [
        ("gold.fct_orders", set(), True),  # no orders yet: an empty mart is right
        ("gold.fct_orders", {"bronze.orders"}, False),  # bronze has orders, the mart has none: silver lost its input
        ("gold.fct_orders", {"gold.fct_orders", "bronze.orders"}, True),
        ("gold.mart_customer_ltv", set(), True),  # filtered mart (paid orders only): judged by its commit alone
    ],
)
def test_a_mart_must_have_rows_only_when_its_source_does(table, non_empty, fresh) -> None:
    commit, has_rows, _ = commit_and_rows(non_empty)

    newest = newest_data_commit(table, commit, has_rows, ROWS_FROM)

    assert (newest == NOW - 60) if fresh else (newest is None)


def test_marts_without_a_source_never_query_rows() -> None:
    commit, has_rows, asked = commit_and_rows(set())

    newest_data_commit("gold.mart_customer_ltv", commit, has_rows, ROWS_FROM)

    assert asked == []


def test_success_publishes_age_and_timestamps(registry, metrics) -> None:
    probe_once(["bronze.orders"], lambda _: NOW - 42, metrics.freshness, metrics, now=lambda: NOW)

    assert sample(registry, "data_freshness_seconds", "bronze.orders") == pytest.approx(42)
    assert sample(registry, "freshness_probe_success", "bronze.orders") == 1
    assert sample(registry, "freshness_last_success_timestamp", "bronze.orders") == NOW


def test_refresh_age_goes_to_its_own_metric(registry, metrics) -> None:
    probe_once(["gold.fct_orders"], lambda _: NOW - 600, metrics.refresh_age, metrics, now=lambda: NOW)

    assert sample(registry, "data_refresh_age_seconds", "gold.fct_orders") == pytest.approx(600)
    assert sample(registry, "data_freshness_seconds", "gold.fct_orders") is None
    assert sample(registry, "freshness_probe_success", "gold.fct_orders") == 1


def test_future_timestamp_is_clamped_to_zero(registry, metrics) -> None:
    probe_once(["bronze.orders"], lambda _: NOW + 5, metrics.freshness, metrics, now=lambda: NOW)

    assert sample(registry, "data_freshness_seconds", "bronze.orders") == 0


def test_empty_table_is_a_failed_probe(registry, metrics) -> None:
    probe_once(["bronze.orders"], lambda _: None, metrics.freshness, metrics, now=lambda: NOW)

    assert sample(registry, "freshness_probe_success", "bronze.orders") == 0
    assert sample(registry, "data_freshness_seconds", "bronze.orders") is None


def test_empty_mart_with_rows_in_its_source_is_a_failed_probe(registry, metrics) -> None:
    commit, has_rows, _ = commit_and_rows({"bronze.orders"})

    def newest(table: str) -> float | None:
        return newest_data_commit(table, commit, has_rows, ROWS_FROM)

    probe_once(["gold.fct_orders"], newest, metrics.refresh_age, metrics, now=lambda: NOW)

    assert sample(registry, "freshness_probe_success", "gold.fct_orders") == 0
    assert sample(registry, "data_refresh_age_seconds", "gold.fct_orders") is None


@pytest.mark.parametrize("gauge", ["freshness", "refresh_age"])
def test_failure_after_success_removes_age_but_keeps_last_success(registry, metrics, gauge) -> None:
    age = getattr(metrics, gauge)
    probe_once(["schema.table"], lambda _: NOW, age, metrics, now=lambda: NOW)

    def broken(_: str) -> float:
        raise ConnectionError("trino unreachable")

    probe_once(["schema.table"], broken, age, metrics, now=lambda: NOW + 60)

    assert sample(registry, "freshness_probe_success", "schema.table") == 0
    assert age.collect()[0].samples == []
    assert sample(registry, "freshness_last_success_timestamp", "schema.table") == NOW


def test_one_failing_table_does_not_block_the_others(registry, metrics) -> None:
    def query(table: str) -> float:
        if table == "bronze.payments":
            raise TimeoutError("query timed out")
        return NOW - 10

    probe_once(["bronze.payments", "bronze.orders"], query, metrics.freshness, metrics, now=lambda: NOW)

    assert sample(registry, "freshness_probe_success", "bronze.payments") == 0
    assert sample(registry, "freshness_probe_success", "bronze.orders") == 1
    assert sample(registry, "data_freshness_seconds", "bronze.orders") == pytest.approx(10)
