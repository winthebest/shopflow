"""JSON-lines logging with the key names of the shopflow log contract (docs/contracts/services.md)."""

import json
import logging
from datetime import UTC, datetime

SERVICE_NAME = "freshness-exporter"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": SERVICE_NAME,
            # The exporter has no tracing; the contract requires the keys with empty values.
            "trace_id": "",
            "span_id": "",
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
