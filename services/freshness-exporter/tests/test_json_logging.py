import json
import logging

from freshness_exporter.json_logging import JsonFormatter


def test_log_line_has_the_contract_keys() -> None:
    record = logging.LogRecord("freshness_exporter", logging.WARNING, __file__, 1, "probe failed for %s", ("t",), None)

    entry = json.loads(JsonFormatter().format(record))

    assert entry["level"] == "WARNING"
    assert entry["message"] == "probe failed for t"
    assert entry["service"] == "freshness-exporter"
    assert entry["trace_id"] == entry["span_id"] == ""
    assert entry["timestamp"].endswith("+00:00")
