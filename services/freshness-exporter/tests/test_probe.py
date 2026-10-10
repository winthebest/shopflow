import pytest
from prometheus_client import CollectorRegistry

from freshness_exporter.probe import FreshnessMetrics, latest_data_commit_query, max_source_ts_query, probe_once

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
