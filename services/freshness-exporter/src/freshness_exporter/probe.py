"""One probe cycle: read max(_source_ts_ms) per table and publish freshness gauges.

A failed probe (query error, timeout, empty table) sets freshness_probe_success{table}=0 and removes
data_freshness_seconds{table}, so a broken probe can never look fresh: the SLO counts the minute as bad and
the absent-metric alert can fire.
"""

import contextlib
import logging
import time
from collections.abc import Callable, Iterable

from prometheus_client import CollectorRegistry, Gauge

LOG = logging.getLogger(__name__)

# Returns max(_source_ts_ms) of a "<schema>.<table>", or None when the table has no rows.
MaxSourceTs = Callable[[str], int | None]


def max_source_ts_query(catalog: str, table: str) -> str:
    """SQL for the newest source commit time in a table.

    Identifiers cannot be bound as query parameters; config.parse_catalog/parse_tables only admit plain lowercase
    identifiers, which are then double-quoted here.
    """
    schema, name = table.split(".")
    return f'SELECT max("_source_ts_ms") FROM "{catalog}"."{schema}"."{name}"'  # noqa: S608


class FreshnessMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        labels = ["table"]
        self.freshness = Gauge(
            "data_freshness_seconds",
            "Seconds between the probe and the newest source change visible in the table",
            labels,
            registry=registry,
        )
        self.success = Gauge(
            "freshness_probe_success",
            "1 if the last probe of the table succeeded, 0 otherwise",
            labels,
            registry=registry,
        )
        self.last_success = Gauge(
            "freshness_last_success_timestamp",
            "Unix time (seconds) of the last successful probe of the table",
            labels,
            registry=registry,
        )


def probe_once(
    tables: Iterable[str],
    max_source_ts: MaxSourceTs,
    metrics: FreshnessMetrics,
    now: Callable[[], float] = time.time,
) -> None:
    for table in tables:
        try:
            newest_ms = max_source_ts(table)
            if newest_ms is None:
                raise LookupError("table has no rows")
        except Exception:
            LOG.exception("freshness probe failed for %s", table)
            metrics.success.labels(table).set(0)
            with contextlib.suppress(KeyError):
                metrics.freshness.remove(table)
            continue

        probed_at = now()
        # Clamp clock skew between Postgres and this pod to 0 instead of reporting negative staleness.
        metrics.freshness.labels(table).set(max(0.0, probed_at - newest_ms / 1000))
        metrics.success.labels(table).set(1)
        metrics.last_success.labels(table).set(probed_at)
