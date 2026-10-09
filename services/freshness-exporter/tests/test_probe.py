import pytest
from prometheus_client import CollectorRegistry

from freshness_exporter.probe import FreshnessMetrics, max_source_ts_query, probe_once

NOW = 1_800_000_000.0


def sample(registry: CollectorRegistry, name: str, table: str) -> float | None:
    return registry.get_sample_value(name, {"table": table})


@pytest.fixture
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry) -> FreshnessMetrics:
    return FreshnessMetrics(registry)


def test_query_quotes_catalog_schema_and_table() -> None:
    assert max_source_ts_query("lake_ro", "bronze.orders") == (
        'SELECT max("_source_ts_ms") FROM "lake_ro"."bronze"."orders"'
    )


def test_success_publishes_freshness_and_timestamps(registry, metrics) -> None:
    probe_once(["bronze.orders"], lambda _: int((NOW - 42) * 1000), metrics, now=lambda: NOW)

    assert sample(registry, "data_freshness_seconds", "bronze.orders") == pytest.approx(42)
    assert sample(registry, "freshness_probe_success", "bronze.orders") == 1
    assert sample(registry, "freshness_last_success_timestamp", "bronze.orders") == NOW


def test_future_source_timestamp_is_clamped_to_zero(registry, metrics) -> None:
    probe_once(["bronze.orders"], lambda _: int((NOW + 5) * 1000), metrics, now=lambda: NOW)

    assert sample(registry, "data_freshness_seconds", "bronze.orders") == 0


def test_empty_table_is_a_failed_probe(registry, metrics) -> None:
    probe_once(["bronze.orders"], lambda _: None, metrics, now=lambda: NOW)

    assert sample(registry, "freshness_probe_success", "bronze.orders") == 0
    assert sample(registry, "data_freshness_seconds", "bronze.orders") is None


def test_failure_after_success_removes_freshness_but_keeps_last_success(registry, metrics) -> None:
    probe_once(["bronze.orders"], lambda _: int(NOW * 1000), metrics, now=lambda: NOW)

    def broken(_: str) -> int:
        raise ConnectionError("trino unreachable")

    probe_once(["bronze.orders"], broken, metrics, now=lambda: NOW + 60)

    assert sample(registry, "freshness_probe_success", "bronze.orders") == 0
    assert sample(registry, "data_freshness_seconds", "bronze.orders") is None
    assert sample(registry, "freshness_last_success_timestamp", "bronze.orders") == NOW


def test_one_failing_table_does_not_block_the_others(registry, metrics) -> None:
    def query(table: str) -> int:
        if table == "bronze.payments":
            raise TimeoutError("query timed out")
        return int((NOW - 10) * 1000)

    probe_once(["bronze.payments", "bronze.orders"], query, metrics, now=lambda: NOW)

    assert sample(registry, "freshness_probe_success", "bronze.payments") == 0
    assert sample(registry, "freshness_probe_success", "bronze.orders") == 1
    assert sample(registry, "data_freshness_seconds", "bronze.orders") == pytest.approx(10)
