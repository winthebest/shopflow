"""Entry point: serve /metrics and probe every PROBE_INTERVAL_SECONDS until SIGTERM."""

import logging
import os
import signal
import threading
from collections.abc import Callable

import trino
from prometheus_client import REGISTRY, start_http_server

from freshness_exporter import config, json_logging
from freshness_exporter.probe import (
    FreshnessMetrics,
    NewestSeconds,
    latest_data_commit_query,
    max_source_ts_query,
    probe_once,
)

LOG = logging.getLogger("freshness_exporter")


def trino_scalar(cfg: config.Config) -> Callable[[str], float | None]:
    """Run one single-value query on Trino over HTTPS with password auth; one short-lived connection per probe."""

    def query(sql: str) -> float | None:
        with trino.dbapi.connect(
            host=cfg.trino_host,
            port=cfg.trino_port,
            user=cfg.trino_user,
            catalog=cfg.trino_catalog,
            http_scheme="https",
            auth=trino.auth.BasicAuthentication(cfg.trino_user, cfg.trino_password),
            verify=cfg.trino_ca_file,
            request_timeout=cfg.query_timeout_seconds,
        ) as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            row = cursor.fetchone()
        return None if row is None or row[0] is None else float(row[0])

    return query


def newest_source_change(cfg: config.Config, scalar: Callable[[str], float | None]) -> NewestSeconds:
    def newest(table: str) -> float | None:
        ms = scalar(max_source_ts_query(cfg.trino_catalog, table))
        return None if ms is None else ms / 1000

    return newest


def latest_data_commit(cfg: config.Config, scalar: Callable[[str], float | None]) -> NewestSeconds:
    return lambda table: scalar(latest_data_commit_query(cfg.trino_catalog, table))


def main() -> None:
    json_logging.configure(os.environ.get("LOG_LEVEL", "INFO"))
    cfg = config.load()
    # The default registry also exports process_start_time_seconds, used to inhibit freshness alerts after restarts.
    metrics = FreshnessMetrics(REGISTRY)
    start_http_server(cfg.metrics_port)
    LOG.info(
        "serving metrics on :%d, probing %d tables (source time) and %d tables (data commit) every %ss",
        cfg.metrics_port,
        len(cfg.tables),
        len(cfg.refresh_tables),
        cfg.probe_interval_seconds,
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    scalar = trino_scalar(cfg)
    source_change, data_commit = newest_source_change(cfg, scalar), latest_data_commit(cfg, scalar)
    while not stop.is_set():
        probe_once(cfg.tables, source_change, metrics.freshness, metrics)
        probe_once(cfg.refresh_tables, data_commit, metrics.refresh_age, metrics)
        stop.wait(cfg.probe_interval_seconds)


if __name__ == "__main__":
    main()
