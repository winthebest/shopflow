"""Connections to the lab stack's Trino as one identity; credentials and certificate from $LAB_RUN_DIR."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import trino

REPO = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get("LAB_RUN_DIR", REPO / "out/lab"))
URL = urlsplit(os.environ.get("LAB_TRINO_URL", "https://127.0.0.1:28443"))


def connect(user: str) -> trino.dbapi.Connection:
    return trino.dbapi.connect(
        host=URL.hostname,
        port=URL.port,
        http_scheme="https",
        user=user,
        auth=trino.auth.BasicAuthentication(user, (RUN / "passwords" / user).read_text()),
        verify=str(RUN / "trino/tls/cert.pem"),
    )


def query(conn: trino.dbapi.Connection, sql: str) -> list:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()
