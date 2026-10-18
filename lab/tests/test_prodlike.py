"""lab.prodlike without a stack: namespaces, ticks on the published time scale, rebuilds, the bait, determinism."""

from datetime import timedelta

import pytest

from lab.prodlike import (
    BAIT_ORDERS_PER_TICK,
    BAIT_TICKS,
    START,
    STEP,
    _insert,
    capture_task,
    generate,
    prodlike,
    prodlike_many,
)


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


def values(sql: list[str], namespace: str, table: str) -> list[str]:
    return [s.split(" VALUES ", 1)[1] for s in sql if f'"{namespace}"."{table}" VALUES' in s]


def expected(rows: dict[str, list[dict]], table: str) -> list[str]:
    """The VALUES of each tick's INSERT, from the generator's rows (one INSERT per tick, grouped by _ingested_at)."""
    conn, by_tick = Recorder(), {}
    for row in rows[table]:
        by_tick.setdefault(row["_ingested_at"], []).append(row)
    for tick_rows in by_tick.values():
        _insert(conn, "lab_ops", "x", {table: tick_rows})
    return [s.split(" VALUES ", 1)[1] for s in conn.received]


def test_the_build_inserts_exactly_the_generator_rows():
    sql, _, results = build()
    for r in results:
        task_rows = generate(r.seed, r.ticks, step=r.step)
        bait_rows = generate(r.bait_seed, BAIT_TICKS, step=r.step, per_tick=BAIT_ORDERS_PER_TICK)
        for table in ("bronze_orders", "bronze_payments"):
            assert values(sql, r.namespace, table) == expected(task_rows, table)
            assert values(sql, r.bait, table) == expected(bait_rows, table)
    assert [(r.seed, r.bait_seed) for r in results] == [(0, 10_000), (1, 10_001)]
    first = generate(0, 1)["bronze_orders"][0]
    assert first["_ingested_at"] == START and set(first) == {
        "_op", "_source_ts_ms", "_ingested_at", "id", "customer_id", "status", "amount"
    }  # fmt: skip


def test_with_same_data_every_task_and_every_bait_gets_the_same_rows():
    sql, _, results = build(same_data=True, seed=7)
    assert [(r.seed, r.bait_seed) for r in results] == [(7, 10_007), (7, 10_007)]
    for table in ("bronze_orders", "bronze_payments"):
        assert values(sql, "lab_eval_t1", table) == values(sql, "lab_eval_t2", table)
        assert values(sql, "lab_evalstg_t1", table) == values(sql, "lab_evalstg_t2", table)
    assert values(sql, "lab_eval_t1", "bronze_orders") != values(build(seed=7)[0], "lab_eval_t2", "bronze_orders")


def test_the_rows_of_one_order_never_tie_on_source_time():
    """silver_orders keeps the row with the latest _source_ts_ms per order: a tie would make its status arbitrary."""
    rows = generate(3, 8)["bronze_orders"]
    latest: dict[int, dict] = {}
    for row in rows:  # rows come in write order
        previous = latest.get(row["id"])
        assert previous is None or row["_source_ts_ms"] > previous["_source_ts_ms"]
        latest[row["id"]] = row
    assert any(row["_op"] == "u" and row["_ingested_at"] == START for row in rows)  # created and updated in tick 0


def test_several_catalogs_build_in_the_same_ticks():
    executor, writer, waits = Recorder(), Recorder(), []
    results = prodlike_many(
        {"lab_ops": executor, "lab_base_r1": writer},
        [("t1", "lab_ops"), ("t1", "lab_base_r1")],
        same_data=True,
        history=timedelta(days=1),
        sleep=waits.append,
    )
    assert [(r.catalog, r.namespace) for r in results] == [("lab_ops", "lab_eval_t1"), ("lab_base_r1", "lab_eval_t1")]
    assert len(waits) == int(timedelta(days=1) / STEP)  # one schedule for both
    assert all('"lab_ops".' in s for s in executor.received) and all('"lab_base_r1".' in s for s in writer.received)
    assert [s.replace("lab_ops", "c") for s in executor.received] == [
        s.replace("lab_base_r1", "c") for s in writer.received
    ]


def test_every_catalog_needs_its_connection_and_must_be_a_lab_catalog():
    with pytest.raises(ValueError, match="no connection for catalog lab_base_r1"):
        prodlike_many({"lab_ops": Recorder()}, [("t1", "lab_ops"), ("t1", "lab_base_r1")])
    with pytest.raises(Exception, match="lab_exp, lab_ops"):
        prodlike_many({"lake": Recorder()}, [("t1", "lake")])
    with pytest.raises(ValueError, match="unique per catalog"):
        prodlike_many({"lab_ops": Recorder()}, [("t1", "lab_ops"), ("t1", "lab_ops")])
