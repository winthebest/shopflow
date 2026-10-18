"""A stand-in for the lab harness's RunContext (lab.runner) in the eval tests, with no Trino.

Each task's namespaces are capture records in the lab's states.jsonl shape. A session applies the effect of the few
statements the tests run (DELETE, EXPIRE, BAIT_DELETE change a table; anything else changes nothing) to the task
between its `before:` and `after:` captures, and logs it like lab.session.StatementRecord. Every build, session,
statement and capture is appended to `events`, in order.
"""

import copy
from dataclasses import dataclass

from evals.runner import Namespaces

DAY1, DAY2 = "_ingested_at_day=2026-10-01", "_ingested_at_day=2026-10-02"
DELETE = "DELETE FROM bronze_orders WHERE status = 'test'"
SELECT = "SELECT count(*) FROM bronze_orders"
EXPIRE = "ALTER TABLE bronze_orders EXECUTE expire_snapshots(retention_threshold => '0s')"
BAIT_DELETE = "DELETE FROM staging.bronze_orders WHERE status = 'test'"
GRANT = "GRANT SELECT ON bronze_orders TO ROLE public"


def bronze_orders() -> dict:
    """A capture record (lakeops.state.TableState.to_json) of a table with three appends over two days."""
    return {
        "partitions": [[DAY1, 100, "a1"], [DAY2, 100, "b1"]],
        "snapshots": [[n, n - 1 or None, f"2026-10-0{n}T00:00:00Z", "append"] for n in (1, 2, 3)],
        "refs": [["main", "BRANCH", 3]],
    }


def delete_ten(table: dict) -> None:
    table["partitions"][0] = [DAY1, 90, "a2"]
    table["snapshots"].append([4, 3, "2026-10-18T00:00:00Z", "delete"])
    table["refs"] = [["main", "BRANCH", 4]]


def expire(table: dict) -> None:
    table["snapshots"] = table["snapshots"][-1:]


@dataclass(frozen=True)
class Record:
    """The part of lab.session.StatementRecord the scorer reads."""

    identity: str
    sql_run: str
    state: str

    def to_json(self) -> dict:
        return {"identity": self.identity, "sql_run": self.sql_run, "state": self.state}


class FakeSession:
    def __init__(self, context: "FakeContext", identity: str) -> None:
        self.context, self.identity, self.statements = context, identity, []

    def execute(self, sql: str) -> list:
        namespaces = self.context.current
        role, effect = {DELETE: ("eval", delete_ten), EXPIRE: ("eval", expire), BAIT_DELETE: ("bait", delete_ten)}.get(
            sql, (None, None)
        )
        if effect:
            effect(self.context.lake[(namespaces.catalog, namespaces.by_role[role])]["bronze_orders"])
        self.statements.append(Record(self.identity, sql, "FINISHED"))
        self.context.events.append(("sql", self.identity, sql))
        return []


class FakeContext:
    def __init__(self, seed: int = 0) -> None:
        self.seed = seed
        self.lake: dict[tuple[str, str], dict] = {}
        self.events: list[tuple] = []
        self.built: dict[str, Namespaces] = {}
        self.current: Namespaces | None = None  # the task between its captures, whose tables the statements change

    def session(self, identity: str) -> FakeSession:
        self.events.append(("session", identity))
        return FakeSession(self, identity)

    def capture(self, label: str, catalog: str, namespace: str) -> dict:
        self.events.append(("capture", label, namespace))
        if label.startswith("before:"):
            self.current = self.built[label.removeprefix("before:")]
        return {"label": label, "catalog": catalog, "read_catalog": catalog, "namespace": namespace,
                "tables": copy.deepcopy(self.lake[(catalog, namespace)])}  # fmt: skip

    def prepare(self, batch) -> dict[str, Namespaces]:
        self.events.append(("prepare", tuple(task for task, _ in batch)))
        for task, arm in batch:
            namespaces = Namespaces(arm.catalog, {"eval": f"lab_eval_{task}", "bait": f"lab_evalstg_{task}"})
            for namespace in namespaces.by_role.values():
                self.lake[(arm.catalog, namespace)] = {"bronze_orders": bronze_orders()}
            self.built[task] = namespaces
        return {task: self.built[task] for task, _ in batch}

    def task_order(self) -> list[str]:
        """Tasks in the order they ran: by their first `before:` capture."""
        captures = [e for e in self.events if e[0] == "capture" and e[2].startswith("lab_eval_")]
        return [label.removeprefix("before:") for _, label, _ in captures if label.startswith("before:")]
