"""The direct (PyIceberg) writers against an in-memory stand-in for a PyIceberg table: what they read, what they
commit, how they log it. Whether the real catalog lets a racing add be lost is measured on the lab (question 6)."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from e7_concurrency import direct
from e7_concurrency.oplog import OpLogWriter, Outcome, read_oplog
from e7_concurrency.oracle import EventRow, grade
from e7_concurrency.scenario import BASELINE, OpSpec, Scenario
from pyiceberg.exceptions import CommitFailedException

COUNTERS = pa.schema(
    [
        pa.field("key", pa.int64(), nullable=False),
        pa.field("value", pa.int64(), nullable=False),
        pa.field("applied", pa.list_(pa.field("element", pa.string(), nullable=False)), nullable=False),
    ]
)
EVENTS = pa.schema(
    [
        pa.field("writer_id", pa.string(), nullable=False),
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("key", pa.int64(), nullable=False),
        pa.field("delta", pa.int64(), nullable=False),
        pa.field("event_day", pa.date32(), nullable=False),
    ]
)


class FakeTable:
    def __init__(self, schema: pa.Schema, rows: list[dict] | None = None) -> None:
        self._schema, self.rows, self.failures = schema, list(rows or []), []

    def refresh(self) -> None:
        pass

    def schema(self) -> SimpleNamespace:
        return SimpleNamespace(as_arrow=lambda: self._schema)

    def scan(self, row_filter: str) -> SimpleNamespace:
        column, value = (part.strip() for part in row_filter.split("="))
        matching = [r for r in self.rows if r[column] == int(value)]
        return SimpleNamespace(to_arrow=lambda: pa.Table.from_pylist(matching, schema=self._schema))

    def _fail(self) -> None:
        if self.failures:
            raise self.failures.pop(0)

    def upsert(self, df: pa.Table, join_cols: list[str]) -> None:
        self._fail()
        assert df.schema == self._schema
        new = df.to_pylist()
        keys = {tuple(r[c] for c in join_cols) for r in new}
        self.rows = [r for r in self.rows if tuple(r[c] for c in join_cols) not in keys] + new

    def append(self, df: pa.Table) -> None:
        self._fail()
        assert df.schema == self._schema
        self.rows += df.to_pylist()


def w2(**overrides) -> Scenario:
    return Scenario(name="t", workload="w2", writers=2, contention="high", seed=1, **overrides)


def test_w2_py_reads_the_row_adds_and_upserts_it_back(monkeypatch: pytest.MonkeyPatch):
    table = FakeTable(COUNTERS, [{"key": k, "value": 0, "applied": []} for k in range(4)])
    monkeypatch.setattr(direct, "lab_catalog", lambda: SimpleNamespace(load_table=lambda name: table))
    add = direct.pyiceberg_writes(None, w2(), direct.W2_PY, "w2-0")
    assert add("w2-0:1", OpSpec(1, 2, 5), date(2026, 1, 1)).outcome is Outcome.ACKED
    assert add("w2-0:2", OpSpec(2, 2, 3), date(2026, 1, 1)).outcome is Outcome.ACKED
    assert [r for r in table.rows if r["key"] == 2] == [{"key": 2, "value": 8, "applied": ["w2-0:1", "w2-0:2"]}]


def test_w2_py_logs_a_lost_commit_race_by_its_class(monkeypatch: pytest.MonkeyPatch):
    table = FakeTable(COUNTERS, [{"key": 0, "value": 0, "applied": []}])
    table.failures = [CommitFailedException("Requirement failed: branch main has changed")]
    monkeypatch.setattr(direct, "lab_catalog", lambda: SimpleNamespace(load_table=lambda name: table))
    result = direct.pyiceberg_writes(None, w2(), direct.W2_PY, "w2-0")("w2-0:1", OpSpec(1, 0, 1), date(2026, 1, 1))
    assert (result.outcome, result.error) == (Outcome.UNKNOWN, "CommitFailedException")
    assert table.rows == [{"key": 0, "value": 0, "applied": []}]


def test_w2_py_needs_counters_with_the_ledger():
    with pytest.raises(ValueError, match="ledger"):
        direct.pyiceberg_writes(None, w2(ledger=False), direct.W2_PY, "w2-0")


def test_direct_writers_load_only_the_lab_catalog_tables():
    loaded = []
    catalog = SimpleNamespace(load_table=lambda name: loaded.append(name) or name)
    assert direct.load("lab_exp.lab_e7.events", lambda: catalog) == "lab_e7.events"
    with pytest.raises(ValueError, match="only to lab_exp"):
        direct.load("lake.bronze.orders", lambda: catalog)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_the_appender_commits_whole_batches_as_groups(tmp_path: Path):
    scenario = Scenario(name="t", workload="w1", writers=2, contention="medium", seed=1, appender_batch=3)
    table, clock = FakeTable(EVENTS), Clock()
    with OpLogWriter(tmp_path / "sink.jsonl", "sink") as log:
        sent = direct.run_appender(
            table, scenario=scenario, log=log, start=0.0, deadline=2.5, clock=clock, sleep=clock.sleep
        )
    ops = read_oplog([tmp_path / "sink.jsonl"])
    assert sent == 3  # at 0, 1 and 2 s
    assert [o.group for o in ops] == ["sink:commit-1"] * 3 + ["sink:commit-2"] * 3 + ["sink:commit-3"] * 3
    events = [EventRow(r["writer_id"], r["seq"], r["key"], r["delta"]) for r in table.rows]
    verdict = grade(ops, events, [], seeded_keys=[])
    assert (verdict.valid, verdict.torn_groups, verdict.acked) == (True, 0, 9)


def test_a_failed_append_leaves_its_whole_batch_with_one_outcome(tmp_path: Path):
    scenario = Scenario(name="t", workload="w1", writers=2, contention="medium", seed=1, appender_batch=2)
    table, clock = FakeTable(EVENTS), Clock()
    table.failures = [CommitFailedException("conflict")]
    with OpLogWriter(tmp_path / "sink.jsonl", "sink") as log:
        direct.run_appender(table, scenario=scenario, log=log, start=0.0, deadline=1.5, clock=clock, sleep=clock.sleep)
    ops = read_oplog([tmp_path / "sink.jsonl"])
    assert [(o.group, o.outcome, o.error) for o in ops] == [
        ("sink:commit-1", Outcome.UNKNOWN, "CommitFailedException"),
        ("sink:commit-1", Outcome.UNKNOWN, "CommitFailedException"),
        ("sink:commit-2", Outcome.ACKED, None),
        ("sink:commit-2", Outcome.ACKED, None),
    ]


def test_the_appender_needs_the_events_workload():
    with pytest.raises(ValueError, match="appender"):
        Scenario(name="t", workload="w2", writers=2, contention="medium", seed=1, appender_batch=2)
    assert BASELINE.writes is None
