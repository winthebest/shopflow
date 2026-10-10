"""One probe cycle: read the newest timestamp per table and publish its age.

Two measures, each for its own table list:
- data_freshness_seconds: now - max(_source_ts_ms), the source commit time of the newest change (bronze, CDC lag);
- data_refresh_age_seconds: now - the latest data commit of the table (`$snapshots`, gold, rebuilt by dbt).

A failed probe (query error, timeout, empty table) sets freshness_probe_success{table}=0 and removes the age sample,
so a broken probe can never look fresh: the SLO counts the minute as bad and the absent-metric alert can fire.
"""

import contextlib
import logging
import time
from collections.abc import Callable, Iterable

from prometheus_client import CollectorRegistry, Gauge

LOG = logging.getLogger(__name__)

# Returns the newest timestamp of a "<schema>.<table>" in Unix seconds, or None when there is none.
NewestSeconds = Callable[[str], float | None]


def _relation(catalog: str, table: str, suffix: str = "") -> str:
    """Quoted catalog.schema.table. Identifiers cannot be bound as query parameters; config.parse_catalog and
    config.parse_tables only admit plain lowercase identifiers, which are then double-quoted here."""
    schema, name = table.split(".")
    return f'"{catalog}"."{schema}"."{name}{suffix}"'


def max_source_ts_query(catalog: str, table: str) -> str:
    """SQL for the newest source commit time in a table, in epoch milliseconds."""
    return f'SELECT max("_source_ts_ms") FROM {_relation(catalog, table)}'  # noqa: S608


def latest_data_commit_query(catalog: str, table: str) -> str:
    """SQL for the time of the table's latest data commit, in Unix seconds.

    `replace` snapshots (optimize, manifest rewrites by the daily Iceberg maintenance) change no rows: counting them
    would make a table look refreshed while its producer (dbt) is stopped.
    """
    return (
        f"SELECT to_unixtime(max(committed_at)) FROM {_relation(catalog, table, '$snapshots')} "  # noqa: S608
        "WHERE operation <> 'replace'"
    )


class FreshnessMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        labels = ["table"]
        self.freshness = Gauge(
            "data_freshness_seconds",
            "Seconds between the probe and the newest source change visible in the table",
            labels,
            registry=registry,
        )
        self.refresh_age = Gauge(
            "data_refresh_age_seconds",
            "Seconds between the probe and the table's latest data commit (replace snapshots excluded)",
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
    newest_seconds: NewestSeconds,
    age: Gauge,
    metrics: FreshnessMetrics,
    now: Callable[[], float] = time.time,
) -> None:
    """Probe every table and set `age` (one of the metrics' age gauges) to now - its newest timestamp."""
    for table in tables:
        try:
            newest = newest_seconds(table)
            if newest is None:
                raise LookupError("table has no rows")
        except Exception:
            LOG.exception("freshness probe failed for %s", table)
            metrics.success.labels(table).set(0)
            with contextlib.suppress(KeyError):
                age.remove(table)
            continue

        probed_at = now()
        # Clamp clock skew between the source and this pod to 0 instead of reporting a negative age.
        age.labels(table).set(max(0.0, probed_at - newest))
        metrics.success.labels(table).set(1)
        metrics.last_success.labels(table).set(probed_at)
