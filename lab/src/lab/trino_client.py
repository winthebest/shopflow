"""Connections to the lab's Trino as one identity.

Defaults are the lab stack's: https://127.0.0.1:28443, credentials and certificate from $LAB_RUN_DIR (out/lab, written
by lab/stack.sh). Another Trino (e.g. the platform's in k3d) is reached with LAB_TRINO_URL, LAB_TRINO_CA and
LAB_TRINO_PASSWORD_DIR (one file per user); lab.guard decides what may run there. Query measurements come from
Trino itself (query_stats: GET /v1/query/{id}), or, once the coordinator has dropped the query, from
system.runtime.queries (query_times).
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

import requests
import trino

REPO = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    url: SplitResult
    run_dir: Path
    ca: Path
    password_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        run_dir = Path(os.environ.get("LAB_RUN_DIR", REPO / "out/lab"))
        return cls(
            url=urlsplit(os.environ.get("LAB_TRINO_URL", "https://127.0.0.1:28443")),
            run_dir=run_dir,
            ca=Path(os.environ.get("LAB_TRINO_CA", run_dir / "trino/tls/cert.pem")),
            password_dir=Path(os.environ.get("LAB_TRINO_PASSWORD_DIR", run_dir / "passwords")),
        )

    def password(self, user: str) -> str:
        return (self.password_dir / user).read_text()


def connect(user: str, settings: Settings, **client_options: object) -> trino.dbapi.Connection:
    """`client_options` go to trino.dbapi.connect. A writer that must not commit twice passes max_attempts=1: by
    default the client resubmits a statement (POST /v1/statement) up to 3 times after a connection error or timeout,
    and a write that reached Trino the first time then runs again."""
    return trino.dbapi.connect(
        host=settings.url.hostname,
        port=settings.url.port,
        http_scheme=settings.url.scheme,
        user=user,
        auth=trino.auth.BasicAuthentication(user, settings.password(user)),
        verify=str(settings.ca),
        **client_options,
    )


def query(conn: trino.dbapi.Connection, sql: str) -> list:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def server_info(user: str, settings: Settings) -> dict:
    """GET /v1/info: version, node environment (`lab` on the lab stack), coordinator state."""
    response = requests.get(
        f"{settings.url.geturl()}/v1/info",
        auth=(user, settings.password(user)),
        verify=str(settings.ca),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h|d)")
_DURATION_SECONDS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600, "d": 86400}
_SIZE = re.compile(r"(\d+(?:\.\d+)?)(B|kB|MB|GB|TB|PB)")
_SIZE_BYTES = {"B": 1, "kB": 2**10, "MB": 2**20, "GB": 2**30, "TB": 2**40, "PB": 2**50}
# queryStats fields of GET /v1/query/{id} (airlift Duration and DataSize strings) -> the name in QueryStats.
STAT_FIELDS = {
    "elapsedTime": "elapsed_s",
    "queuedTime": "queued_s",
    "planningTime": "planning_s",
    "executionTime": "execution_s",
    "totalCpuTime": "cpu_s",
    "physicalInputDataSize": "physical_input_bytes",
    "processedInputDataSize": "processed_input_bytes",
    "peakUserMemoryReservation": "peak_user_memory_bytes",
}


def parse_airlift(value: str) -> float:
    """An airlift Duration ('1.23s', '450.00ms') in seconds, or a DataSize ('12.30MB', '1024B') in bytes."""
    if match := _DURATION.fullmatch(value):
        return float(match.group(1)) * _DURATION_SECONDS[match.group(2)]
    if match := _SIZE.fullmatch(value):
        return float(match.group(1)) * _SIZE_BYTES[match.group(2)]
    raise ValueError(f"not an airlift duration or data size: {value!r}")


def query_stats(query_id: str, user: str, settings: Settings) -> dict[str, float]:
    """Trino's own measurements of a finished query (GET /v1/query/{id}, queryStats), as seconds and bytes.

    A field Trino does not report is left out. The query info stays on the coordinator for a while after the query
    ends (query.min-expire-age); afterwards use query_times().
    """
    response = requests.get(
        f"{settings.url.geturl()}/v1/query/{query_id}",
        auth=(user, settings.password(user)),
        verify=str(settings.ca),
        timeout=30,
    )
    response.raise_for_status()
    stats = response.json()["queryStats"]
    return {name: parse_airlift(stats[field]) for field, name in STAT_FIELDS.items() if field in stats}


def query_times(conn: trino.dbapi.Connection, query_id: str) -> dict[str, float]:
    """The fallback: queued, planning and end-to-end times from system.runtime.queries, in seconds."""
    if not re.fullmatch(r"[0-9a-z_]+", query_id):
        raise ValueError(f"not a Trino query id: {query_id!r}")
    columns = "queued_time_ms, planning_time_ms, date_diff('millisecond', created, \"end\")"
    rows = query(conn, f"SELECT {columns} FROM system.runtime.queries WHERE query_id = '{query_id}'")  # noqa: S608
    if not rows:
        raise LookupError(f"query {query_id} is not in system.runtime.queries")
    queued, planning, elapsed = rows[0]
    return {"queued_s": queued / 1000, "planning_s": planning / 1000, "elapsed_s": elapsed / 1000}


def run_measured(conn: trino.dbapi.Connection, sql: str) -> tuple[list, str]:
    """Rows and the query id, to look up the query's stats afterwards."""
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    return rows, cur.query_id
