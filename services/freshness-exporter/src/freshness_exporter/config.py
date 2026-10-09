"""Runtime configuration, read once from environment variables."""

import os
import re
from dataclasses import dataclass

# The probe interpolates catalog/schema/table names into SQL, so anything but plain lowercase identifiers is
# rejected up front.
_IDENTIFIER = r"[a-z_][a-z0-9_]*"
_CATALOG_NAME = re.compile(_IDENTIFIER)
_TABLE_NAME = re.compile(rf"{_IDENTIFIER}\.{_IDENTIFIER}")


@dataclass(frozen=True)
class Config:
    trino_host: str
    trino_port: int
    trino_user: str
    trino_password: str
    trino_catalog: str
    trino_ca_file: str
    tables: tuple[str, ...]
    probe_interval_seconds: float
    query_timeout_seconds: float
    metrics_port: int


def parse_tables(raw: str) -> tuple[str, ...]:
    tables = tuple(t.strip() for t in raw.split(",") if t.strip())
    if not tables:
        raise ValueError("FRESHNESS_TABLES must list at least one <schema>.<table>")
    invalid = [t for t in tables if not _TABLE_NAME.fullmatch(t)]
    if invalid:
        raise ValueError(f"invalid table names (expected <schema>.<table>, lowercase): {invalid}")
    return tables


def parse_catalog(raw: str) -> str:
    if not _CATALOG_NAME.fullmatch(raw):
        raise ValueError(f"invalid catalog name (expected a lowercase identifier): {raw!r}")
    return raw


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env

    def required(name: str) -> str:
        value = env.get(name, "")
        if not value:
            raise ValueError(f"missing required environment variable {name}")
        return value

    return Config(
        trino_host=required("TRINO_HOST"),
        trino_port=int(env.get("TRINO_PORT", "8443")),
        trino_user=env.get("TRINO_USER", "exporter"),
        trino_password=required("TRINO_PASSWORD"),
        trino_catalog=parse_catalog(env.get("TRINO_CATALOG", "lake_ro")),
        trino_ca_file=required("TRINO_CA_FILE"),
        tables=parse_tables(required("FRESHNESS_TABLES")),
        probe_interval_seconds=float(env.get("PROBE_INTERVAL_SECONDS", "60")),
        query_timeout_seconds=float(env.get("QUERY_TIMEOUT_SECONDS", "30")),
        metrics_port=int(env.get("METRICS_PORT", "8080")),
    )
