"""Connections to the lab stack's Trino as one identity; credentials and certificate from $LAB_RUN_DIR."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import requests
import trino

REPO = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get("LAB_RUN_DIR", REPO / "out/lab"))
URL = urlsplit(os.environ.get("LAB_TRINO_URL", "https://127.0.0.1:28443"))


CERT = str(RUN / "trino/tls/cert.pem")


def password(user: str) -> str:
    return (RUN / "passwords" / user).read_text()


def connect(user: str) -> trino.dbapi.Connection:
    return trino.dbapi.connect(
        host=URL.hostname,
        port=URL.port,
        http_scheme="https",
        user=user,
        auth=trino.auth.BasicAuthentication(user, password(user)),
        verify=CERT,
    )


def rest(user: str, method: str, path: str) -> requests.Response:
    """A call to Trino's REST API (e.g. /v1/query/<id>) as `user`."""
    return requests.request(method, f"{URL.geturl()}{path}", auth=(user, password(user)), verify=CERT, timeout=30)


def query(conn: trino.dbapi.Connection, sql: str) -> list:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()
