"""Unit tests of deploy/platform/metabase/base/provision.py against an in-memory fake of the Metabase API.

Run: python3 -I -m unittest discover -s deploy/platform/metabase/tests (make data-metabase-test). Standard library
only, like the script. The real dashboard.json and SQL files are used, so a broken reference fails here.
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent.parent / "base"
SPEC = importlib.util.spec_from_file_location("provision", BASE / "provision.py")
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)

ADMIN_PASSWORD = "admin-admin-admin"
TRINO_PASSWORD = "trino-trino-trino"
ENV = {
    "ADMIN_EMAIL": "admin@shopflow.local",
    "ADMIN_PASSWORD": ADMIN_PASSWORD,
    "TRINO_USER": "metabase",
    "TRINO_PASSWORD": TRINO_PASSWORD,
    "TRINO_HOST": "trino.lakehouse.svc",
    "TRINO_PORT": "8443",
    "TRINO_CATALOG": "lake_ro",
    "TRINO_SCHEMA": "gold",
    "TRINO_CA_PATH": "/etc/trino-ca/ca.crt",
}
PUBLIC = {("GET", "/api/health"), ("GET", "/api/session/properties"), ("POST", "/api/setup"), ("POST", "/api/session")}


class FakeMetabase:
    """Just enough of Metabase's API: setup and login, databases, collections, cards, dashboards."""

    def __init__(self) -> None:
        self.admin = None
        self.sessions = set()
        self.databases, self.collections, self.cards, self.dashboards = {}, {}, {}, {}
        self.next_id = 100
        self.calls = []

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def __call__(self, method, path, body, session):
        self.calls.append((method, path))
        url = urlparse(path)
        parts = url.path.strip("/").split("/")[1:]  # without "api"
        if (method, url.path) not in PUBLIC and session not in self.sessions:
            return 401, {"message": "Unauthenticated"}
        handler = getattr(self, f"{method.lower()}_{parts[0]}")
        return handler(parts[1:], parse_qs(url.query), body)

    def get_health(self, _rest, _query, _body):
        return 200, {"status": "ok"}

    def get_session(self, _rest, _query, _body):
        return 200, {"has-user-setup": self.admin is not None, "setup-token": None if self.admin else "setup-tok"}

    def post_setup(self, _rest, _query, body):
        if self.admin is not None or body["token"] != "setup-tok":
            return 403, {"message": "setup already done"}
        self.admin = (body["user"]["email"], body["user"]["password"])
        return self.login()

    def post_session(self, _rest, _query, body):
        if (body["username"], body["password"]) != self.admin:
            return 401, {"message": "Password did not match stored password."}
        return self.login()

    def login(self):
        session = f"session-{self.new_id()}"
        self.sessions.add(session)
        return 200, {"id": session}

    def get_database(self, _rest, _query, _body):
        return 200, {"data": list(self.databases.values()), "total": len(self.databases)}

    def post_database(self, _rest, _query, body):
        database = {"id": self.new_id(), **body}
        self.databases[database["id"]] = database
        return 200, database

    def put_database(self, rest, _query, body):
        self.databases[int(rest[0])].update(body)
        return 200, self.databases[int(rest[0])]

    def get_collection(self, rest, query, _body):
        if not rest:
            return 200, list(self.collections.values())
        models = query["models"][0]
        store = self.cards if models == "card" else self.dashboards
        items = [{"id": i, "name": x["name"]} for i, x in store.items() if x["collection_id"] == int(rest[0])]
        return 200, {"data": items, "total": len(items)}

    def post_collection(self, _rest, _query, body):
        collection = {"id": self.new_id(), "archived": False, **body}
        self.collections[collection["id"]] = collection
        return 200, collection

    def post_card(self, _rest, _query, body):
        card = {"id": self.new_id(), **body}
        self.cards[card["id"]] = card
        return 200, card

    def put_card(self, rest, _query, body):
        self.cards[int(rest[0])].update(body)
        return 200, self.cards[int(rest[0])]

    def post_dashboard(self, _rest, _query, body):
        dashboard = {"id": self.new_id(), "dashcards": [], **body}
        self.dashboards[dashboard["id"]] = dashboard
        return 200, dashboard

    def get_dashboard(self, rest, _query, _body):
        return 200, self.dashboards[int(rest[0])]

    def put_dashboard(self, rest, _query, body):
        dashboard = self.dashboards[int(rest[0])]
        dashcards = []
        for dashcard in body["dashcards"]:
            dashcards.append({**dashcard, "id": dashcard["id"] if dashcard["id"] > 0 else self.new_id()})
        dashboard.update({**body, "dashcards": dashcards})
        return 200, dashboard


def run(fake, directory=BASE / "dashboard", env=ENV):
    return provision.provision(provision.Metabase(fake), env, directory)


class ProvisionTest(unittest.TestCase):
    def test_first_run_sets_up_admin_connection_questions_and_dashboard(self):
        fake = FakeMetabase()
        report = run(fake)

        self.assertEqual(report[0], "admin: setup")
        self.assertEqual(fake.admin, (ENV["ADMIN_EMAIL"], ADMIN_PASSWORD))
        self.assertIn(("POST", "/api/setup"), fake.calls)
        self.assertNotIn(("POST", "/api/session"), fake.calls)

        (database,) = fake.databases.values()
        self.assertEqual(database["engine"], "starburst")
        self.assertEqual(database["name"], provision.DATABASE_NAME)
        details = database["details"]
        self.assertEqual((details["host"], details["port"]), ("trino.lakehouse.svc", 8443))
        self.assertEqual((details["catalog"], details["schema"], details["user"]), ("lake_ro", "gold", "metabase"))
        self.assertTrue(details["ssl"])
        self.assertEqual(details["additional-options"], "SSLVerification=FULL&SSLTrustStorePath=/etc/trino-ca/ca.crt")

        (collection,) = fake.collections.values()
        spec = json.loads((BASE / "dashboard" / "dashboard.json").read_text())
        self.assertEqual(len(fake.cards), len(spec["cards"]))
        for card in fake.cards.values():
            self.assertEqual(card["collection_id"], collection["id"])
            self.assertEqual(card["dataset_query"]["type"], "native")
            self.assertEqual(card["dataset_query"]["database"], database["id"])
            self.assertIn("FROM gold.", card["dataset_query"]["native"]["query"])

        (dashboard,) = fake.dashboards.values()
        self.assertEqual(dashboard["name"], spec["name"])
        layout = {(d["card_id"], d["row"], d["col"], d["size_x"], d["size_y"]) for d in dashboard["dashcards"]}
        by_name = {card["name"]: card["id"] for card in fake.cards.values()}
        expected = {
            (by_name[c["name"]], *(c["layout"][k] for k in ("row", "col", "size_x", "size_y"))) for c in spec["cards"]
        }
        self.assertEqual(layout, expected)

    def test_second_run_logs_in_and_updates_without_duplicates(self):
        fake = FakeMetabase()
        run(fake)
        dashcard_ids = sorted(d["id"] for d in next(iter(fake.dashboards.values()))["dashcards"])

        report = run(fake)

        self.assertEqual(report[0], "admin: login")
        self.assertEqual(fake.calls.count(("POST", "/api/setup")), 1)
        self.assertEqual(len(fake.databases), 1)
        self.assertIn("updated", report[1])
        self.assertEqual(len(fake.collections), 1)
        self.assertEqual(len(fake.cards), 3)
        self.assertIn("0 questions created, 3 updated", report[2])
        self.assertEqual(len(fake.dashboards), 1)
        self.assertEqual(sorted(d["id"] for d in next(iter(fake.dashboards.values()))["dashcards"]), dashcard_ids)

    def test_changed_sql_updates_the_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for f in (BASE / "dashboard").iterdir():
                shutil.copy(f, directory / f.name)
            fake = FakeMetabase()
            run(fake, directory)
            (directory / "top-products.sql").write_text("SELECT 1 AS changed\n")

            run(fake, directory)

        queries = {c["name"]: c["dataset_query"]["native"]["query"] for c in fake.cards.values()}
        self.assertEqual(queries["Top products by revenue (30 days)"], "SELECT 1 AS changed")

    def test_questions_made_in_the_ui_are_left_alone(self):
        fake = FakeMetabase()
        run(fake)
        collection_id = next(iter(fake.collections))
        _, ui_card = fake.post_card([], {}, {"name": "Made in the UI", "collection_id": collection_id})

        run(fake)

        self.assertEqual(fake.cards[ui_card["id"]]["name"], "Made in the UI")
        self.assertNotIn("dataset_query", fake.cards[ui_card["id"]])

    def test_wrong_admin_password_fails_loudly_without_printing_it(self):
        fake = FakeMetabase()
        run(fake)
        with self.assertRaises(provision.ApiError) as raised:
            run(fake, env={**ENV, "ADMIN_PASSWORD": "rotated-rotated-rotated"})
        self.assertIn("POST /api/session: HTTP 401", str(raised.exception))
        self.assertNotIn("rotated-rotated-rotated", str(raised.exception))

    def test_report_never_contains_a_password_or_session(self):
        fake = FakeMetabase()
        text = "\n".join(run(fake) + run(fake))
        for secret in (ADMIN_PASSWORD, TRINO_PASSWORD, *fake.sessions):
            self.assertNotIn(secret, text)


class WaitReadyTest(unittest.TestCase):
    def test_retries_connection_errors_and_unhealthy_answers(self):
        answers = [OSError("refused"), (503, None), (200, {"status": "starting"}), (200, {"status": "ok"})]

        def transport(_method, _path, _body, _session):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        provision.wait_ready(transport, deadline=float("inf"), sleep=lambda _s: None)
        self.assertEqual(answers, [])

    def test_gives_up_after_the_deadline(self):
        def transport(_method, _path, _body, _session):
            raise OSError("refused")

        with self.assertRaises(provision.ApiError):
            provision.wait_ready(transport, deadline=0, sleep=lambda _s: None)


if __name__ == "__main__":
    unittest.main()
