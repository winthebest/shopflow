"""Provision Metabase from git: admin, Trino connection, questions and dashboard (docs/adr/0416).

Runs as an Argo CD PostSync Job after the Metabase Deployment is healthy, on every sync, and is idempotent:
- setup: while Metabase has no user, POST /api/setup with the admin from Secret metabase-admin (the setup wizard is
  never left open); afterwards it logs in as that admin;
- database DATABASE_NAME: Trino catalog lake_ro, schema gold, as user metabase, TLS verified against Trino's CA;
  created, or its connection details updated;
- collection COLLECTION_NAME and, inside it, every question and the dashboard of DASHBOARD_DIR/dashboard.json,
  created or updated by name with the SQL of the file it names. Questions and dashboards made in the UI, or outside
  that collection, are left alone.

Env: MB_URL, ADMIN_EMAIL, ADMIN_PASSWORD, TRINO_USER, TRINO_PASSWORD, TRINO_HOST, TRINO_PORT, TRINO_CATALOG,
TRINO_SCHEMA, TRINO_CA_PATH (the CA file inside the Metabase pod), DASHBOARD_DIR. Standard library only. Prints ids
and counts, never a password or a session token.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

DATABASE_NAME = "Shopflow gold (Trino)"
COLLECTION_NAME = "Shopflow (from git)"
SITE_NAME = "Shopflow"
# Metabase answers on /api/health before its API is usable; the first start runs the app DB migrations (minutes).
READY_DEADLINE_SECONDS = 900
POLL_SECONDS = 5


class ApiError(RuntimeError):
    """A Metabase API call failed: method, path and status, plus the start of Metabase's message."""


# A transport: (method, path, body, session) -> (status, parsed JSON or None). Raises OSError when nothing answers.
Transport = Callable[[str, str, dict | None, str | None], tuple[int, object]]


def http_transport(base_url: str) -> Transport:
    def call(method: str, path: str, body: dict | None, session: str | None) -> tuple[int, object]:
        headers = {"Content-Type": "application/json"}
        if session:
            headers["X-Metabase-Session"] = session
        request = urllib.request.Request(  # noqa: S310 (in-cluster http URL from MB_URL)
            f"{base_url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            return error.code, {"message": raw[:300]}

    return call


class Metabase:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.session: str | None = None

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        status, data = self.transport(method, path, body, self.session)
        if status >= 400:
            message = data.get("message", "") if isinstance(data, dict) else ""
            raise ApiError(f"{method} {path}: HTTP {status} {str(message)[:200]}")
        return data


def wait_ready(transport: Transport, deadline: float, sleep: Callable[[float], None] = time.sleep) -> None:
    """Until /api/health says ok. Connection errors are retried too: NetworkPolicies admit a new pod after a delay."""
    while True:
        try:
            status, data = transport("GET", "/api/health", None, None)
            if status == 200 and isinstance(data, dict) and data.get("status") == "ok":
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise ApiError("Metabase did not become healthy in time")
        sleep(POLL_SECONDS)


def sign_in(mb: Metabase, email: str, password: str) -> str:
    """Session of the admin: set up Metabase with it on first start, log in afterwards. Returns what was done."""
    properties = mb.call("GET", "/api/session/properties")
    if not properties.get("has-user-setup"):
        result = mb.call(
            "POST",
            "/api/setup",
            {
                "token": properties["setup-token"],
                "user": {"email": email, "password": password, "first_name": "Shopflow", "last_name": "Admin"},
                "prefs": {"site_name": SITE_NAME},
            },
        )
        mb.session = result["id"]
        return "setup"
    result = mb.call("POST", "/api/session", {"username": email, "password": password})
    mb.session = result["id"]
    return "login"


def trino_details(env: dict[str, str]) -> dict:
    """Connection details of the Starburst (Trino) driver: HTTPS, certificate checked against Trino's CA file."""
    return {
        "host": env["TRINO_HOST"],
        "port": int(env["TRINO_PORT"]),
        "catalog": env["TRINO_CATALOG"],
        "schema": env["TRINO_SCHEMA"],
        "user": env["TRINO_USER"],
        "password": env["TRINO_PASSWORD"],
        "ssl": True,
        "additional-options": f"SSLVerification=FULL&SSLTrustStorePath={env['TRINO_CA_PATH']}",
    }


def ensure_database(mb: Metabase, details: dict) -> tuple[int, str]:
    """Create the Trino database, or update its details (Metabase tests the connection either way)."""
    listing = mb.call("GET", "/api/database")
    databases = listing.get("data", []) if isinstance(listing, dict) else listing
    for database in databases:
        if database["name"] == DATABASE_NAME:
            mb.call("PUT", f"/api/database/{database['id']}", {"details": details})
            return database["id"], "updated"
    created = mb.call(
        "POST",
        "/api/database",
        {"name": DATABASE_NAME, "engine": "starburst", "details": details, "auto_run_queries": True},
    )
    return created["id"], "created"


def ensure_collection(mb: Metabase) -> int:
    for collection in mb.call("GET", "/api/collection"):
        if collection.get("name") == COLLECTION_NAME and not collection.get("archived"):
            return collection["id"]
    created = mb.call(
        "POST",
        "/api/collection",
        {"name": COLLECTION_NAME, "description": "Provisioned from git by the metabase-provision Job."},
    )
    return created["id"]


def items_by_name(mb: Metabase, collection_id: int, model: str) -> dict[str, int]:
    listing = mb.call("GET", f"/api/collection/{collection_id}/items?models={model}")
    items = listing.get("data", []) if isinstance(listing, dict) else listing
    return {item["name"]: item["id"] for item in items}


def ensure_cards(mb: Metabase, collection_id: int, database_id: int, cards: list[dict], directory: Path) -> dict:
    """Every card of dashboard.json as a native SQL question; returns {card name: card id} and counts."""
    existing = items_by_name(mb, collection_id, "card")
    ids, created, updated = {}, 0, 0
    for card in cards:
        body = {
            "name": card["name"],
            "description": card.get("description"),
            "display": card["display"],
            "visualization_settings": card.get("visualization_settings", {}),
            "collection_id": collection_id,
            "dataset_query": {
                "type": "native",
                "database": database_id,
                "native": {"query": (directory / card["sql"]).read_text().strip()},
            },
        }
        if card["name"] in existing:
            card_id = existing[card["name"]]
            mb.call("PUT", f"/api/card/{card_id}", body)
            updated += 1
        else:
            card_id = mb.call("POST", "/api/card", body)["id"]
            created += 1
        ids[card["name"]] = card_id
    return {"ids": ids, "created": created, "updated": updated}


def ensure_dashboard(mb: Metabase, collection_id: int, spec: dict, card_ids: dict[str, int]) -> tuple[int, int]:
    """The dashboard with exactly the layout of dashboard.json; dashcards already showing a card keep their id."""
    existing = items_by_name(mb, collection_id, "dashboard")
    if spec["name"] in existing:
        dashboard_id = existing[spec["name"]]
    else:
        dashboard_id = mb.call("POST", "/api/dashboard", {"name": spec["name"], "collection_id": collection_id})["id"]
    current = mb.call("GET", f"/api/dashboard/{dashboard_id}")
    by_card = {dashcard.get("card_id"): dashcard["id"] for dashcard in current.get("dashcards", [])}
    dashcards = []
    for index, card in enumerate(spec["cards"]):
        card_id = card_ids[card["name"]]
        layout = card["layout"]
        dashcards.append(
            {
                "id": by_card.get(card_id, -(index + 1)),
                "card_id": card_id,
                "row": layout["row"],
                "col": layout["col"],
                "size_x": layout["size_x"],
                "size_y": layout["size_y"],
                "parameter_mappings": [],
                "visualization_settings": {},
            }
        )
    mb.call(
        "PUT",
        f"/api/dashboard/{dashboard_id}",
        {"description": spec.get("description"), "dashcards": dashcards},
    )
    return dashboard_id, len(dashcards)


def provision(mb: Metabase, env: dict[str, str], directory: Path) -> list[str]:
    spec = json.loads((directory / "dashboard.json").read_text())
    report = [f"admin: {sign_in(mb, env['ADMIN_EMAIL'], env['ADMIN_PASSWORD'])}"]
    database_id, action = ensure_database(mb, trino_details(env))
    report.append(f"database {database_id} ({DATABASE_NAME}): {action}")
    collection_id = ensure_collection(mb)
    cards = ensure_cards(mb, collection_id, database_id, spec["cards"], directory)
    report.append(f"collection {collection_id}: {cards['created']} questions created, {cards['updated']} updated")
    dashboard_id, count = ensure_dashboard(mb, collection_id, spec, cards["ids"])
    report.append(f"dashboard {dashboard_id} ({spec['name']}): {count} cards")
    return report


def main() -> int:
    env = dict(os.environ)
    transport = http_transport(env["MB_URL"].rstrip("/"))
    wait_ready(transport, time.monotonic() + READY_DEADLINE_SECONDS)
    for line in provision(Metabase(transport), env, Path(env["DASHBOARD_DIR"])):
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as error:
        print(f"metabase-provision failed: {error}", file=sys.stderr)
        sys.exit(1)
