"""Entry point: serve /metrics and probe every PROBE_INTERVAL_SECONDS until SIGTERM."""

import logging
import os
import signal
import threading

import trino
from prometheus_client import REGISTRY, start_http_server

from freshness_exporter import config, json_logging
from freshness_exporter.probe import FreshnessMetrics, MaxSourceTs, max_source_ts_query, probe_once

LOG = logging.getLogger("freshness_exporter")


def trino_max_source_ts(cfg: config.Config) -> MaxSourceTs:
    """Query Trino over HTTPS with password auth; one short-lived connection per table probe."""

    def query(table: str) -> int | None:
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
            cursor.execute(max_source_ts_query(cfg.trino_catalog, table))
            row = cursor.fetchone()
        return None if row is None or row[0] is None else int(row[0])

    return query


def main() -> None:
    json_logging.configure(os.environ.get("LOG_LEVEL", "INFO"))
    cfg = config.load()
    # The default registry also exports process_start_time_seconds, used to inhibit freshness alerts after restarts.
    metrics = FreshnessMetrics(REGISTRY)
    start_http_server(cfg.metrics_port)
    LOG.info(
        "serving metrics on :%d, probing %d tables every %ss",
        cfg.metrics_port,
        len(cfg.tables),
        cfg.probe_interval_seconds,
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    query = trino_max_source_ts(cfg)
    while not stop.is_set():
        probe_once(cfg.tables, query, metrics)
        stop.wait(cfg.probe_interval_seconds)


if __name__ == "__main__":
    main()
