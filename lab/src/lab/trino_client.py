"""Connections to the lab's Trino as one identity.

Defaults are the lab stack's: https://127.0.0.1:28443, credentials and certificate from $LAB_RUN_DIR (out/lab, written
by lab/stack.sh). Another Trino (e.g. the platform's in k3d) is reached with LAB_TRINO_URL, LAB_TRINO_CA and
LAB_TRINO_PASSWORD_DIR (one file per user); lab.guard decides what may run there.
"""

import os
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
