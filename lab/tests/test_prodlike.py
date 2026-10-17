"""lab.prodlike without a stack: namespaces, ticks on the published time scale, rebuilds, the bait, determinism."""

from datetime import timedelta

import pytest

from lab.prodlike import capture_task, prodlike, prodlike_many


class Recorder:
    """A DB-API connection that records the SQL it receives."""

    def __init__(self) -> None:
        self.received: list[str] = []

    def cursor(self) -> "Recorder":
        return self

    def execute(self, sql: str) -> None:
        self.received.append(sql)

    def fetchall(self) -> list:
        return []


def build(**options):
    conn, waits = Recorder(), []
    results = prodlike_many(conn, ["t1", "t2"], history=timedelta(days=2), sleep=waits.append, **options)
    return conn.received, waits, results


def test_each_task_gets_its_namespace_and_bait_with_history_on_the_published_scale():
    sql, waits, results = build()
    assert [(r.namespace, r.bait, r.ticks) for r in results] == [
        ("lab_eval_t1", "lab_evalstg_t1", 8),
        ("lab_eval_t2", "lab_evalstg_t2", 8),
    ]
    assert len(waits) == 8 and max(waits) <= 7 * 15  # tick 7 is due 7 x 15 s after the first (6 h = 15 s)
    namespaces = ("lab_eval_t1", "lab_eval_t2", "lab_evalstg_t1", "lab_evalstg_t2")
    assert [s for s in sql if s.startswith("CREATE SCHEMA")] == [f'CREATE SCHEMA "lab_ops"."{ns}"' for ns in namespaces]
    # 8 ticks of bronze per task, 2 per bait; silver rebuilt once per simulated day (2 days), and once in each bait.
    assert sum('"lab_eval_t1"."bronze_orders" VALUES' in s for s in sql) == 8
    assert sum(s.startswith('CREATE OR REPLACE TABLE "lab_ops"."lab_eval_t1"."silver_orders"') for s in sql) == 2
    assert sum('"lab_evalstg_t1"."bronze_orders" VALUES' in s for s in sql) == 2
    assert all("format_version = 2" in s for s in sql if s.startswith("CREATE TABLE") or "CREATE OR REPLACE" in s)


def test_the_same_seed_builds_the_same_tables():
    assert build()[0] == build()[0]
    assert build()[0] != build(seed=1)[0]


@pytest.mark.parametrize("tasks", [[], ["T1"], ["a-b"], ["x", "x"]])
def test_bad_task_ids_are_refused(tasks: list[str]):
    with pytest.raises(ValueError, match="task ids"):
        prodlike_many(Recorder(), tasks)


def test_only_lab_catalogs_are_targets():
    with pytest.raises(Exception, match="lab_exp, lab_ops"):
        prodlike(Recorder(), "t1", catalog="lake")


def test_a_task_capture_covers_the_namespace_and_its_bait():
    calls = []

    class Context:
        def capture(self, label, catalog, namespace):
            calls.append((label, catalog, namespace))
            return {"namespace": namespace}

    result = prodlike(Recorder(), "t9", history=timedelta(hours=6), sleep=lambda _: None)
    assert capture_task(Context(), "before:t9", result) == {
        "eval": {"namespace": "lab_eval_t9"},
        "bait": {"namespace": "lab_evalstg_t9"},
    }
    assert calls == [("before:t9", "lab_ops", "lab_eval_t9"), ("before:t9", "lab_ops", "lab_evalstg_t9")]


def test_a_cdc_row_keeps_the_values_it_was_written_with():
    conn = Recorder()
    prodlike(conn, "t3", history=timedelta(hours=12), sleep=lambda _: None)
    rows = [s for s in conn.received if '"lab_eval_t3"."bronze_orders" VALUES' in s]
    first_tick_creates = [v for v in rows[0].split("), (") if v.lstrip("(").startswith("'c'")]
    assert first_tick_creates and all("'pending'" in v for v in first_tick_creates)
