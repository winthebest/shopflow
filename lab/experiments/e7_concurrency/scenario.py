"""Scenarios and arms, and the op stream each writer works through (docs/research/e7-concurrent-writers.md).

The stream is fixed by the scenario's seed, so both arms process the same ops; only how ops are assigned to writers
may differ (the arm's `assign`). An op's `event_day` is not part of the stream: the writer stamps it when it sends
the op (`event_day()`), so the day partitions written move forward as the run goes on, like a real day partition.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

Workload = Literal["w1", "w2"]  # w1: events inserted; w2: counters added to. Maintenance on top makes W3
Contention = Literal["low", "medium", "high"]
Maintenance = Literal["none", "optimize"]

DAY0 = date(2026, 1, 1)
KEYS_PER_WRITER_LOW = 16
KEYS_MEDIUM = 64
KEYS_HIGH = 4
DELTAS = range(1, 10)  # positive, so a run without the ledger can still be bounded (Σ acked ≤ value)


@dataclass(frozen=True)
class OpSpec:
    seq: int
    key: int
    delta: int


@dataclass(frozen=True)
class Scenario:
    name: str
    workload: Workload
    writers: int
    contention: Contention
    seed: int
    seconds: float = 45.0  # write phase
    drain_seconds: float = 30.0  # after the write phase, in-flight ops may finish; writers still open are killed
    ledger: bool = True  # counters carry `applied`; off only to measure the ledger's own cost
    maintenance: Maintenance = "none"
    optimize_every_seconds: float = 5.0
    day_seconds: float = 10.0  # the event day moves forward this often during the write phase
    # A sink-like appender beside the writers (events only): a batch of this many events per commit, through the
    # REST catalog with PyIceberg; 0: none.
    appender_batch: int = 0
    appender_every_seconds: float = 1.0
    ops_per_writer: int = 20_000  # an upper bound: the deadline stops writers long before

    def __post_init__(self) -> None:
        if self.writers < 1:
            raise ValueError("a scenario needs at least one writer")
        if self.contention not in ("low", "medium", "high"):
            raise ValueError(f"unknown contention level {self.contention!r}")
        if not self.ledger and self.workload != "w2":
            raise ValueError("only the counters workload runs without the ledger")
        if self.appender_batch and self.workload != "w1":
            raise ValueError("the sink-like appender writes events: workload w1")

    @property
    def table(self) -> str:
        return "events" if self.workload == "w1" else "counters"

    def seeded_keys(self) -> range:
        if self.contention == "low":
            return range(KEYS_PER_WRITER_LOW * self.writers)
        return range(KEYS_MEDIUM if self.contention == "medium" else KEYS_HIGH)

    def event_day(self, writer: int, elapsed: float) -> date:
        """The day an event sent `elapsed` seconds into the write phase belongs to. At low contention each writer has
        its own range of days, so writers never share a partition."""
        day = int(max(elapsed, 0.0) // self.day_seconds)
        return DAY0 + timedelta(days=day + (1000 * writer if self.contention == "low" else 0))


def round_robin(index: int, _key: int, writers: int) -> int:
    """The default assignment: op i of the shared stream goes to writer i mod N."""
    return index % writers


Assign = Callable[[int, int, int], int]


@dataclass(frozen=True)
class Arm:
    """How the writers are configured; the scenario fixes what they write."""

    name: str
    # The trino client's own HTTP attempts per request. None keeps its default (3): it then re-sends a statement
    # after a timeout or a lost connection, out of the op log's sight.
    client_max_attempts: int | None = None
    request_timeout_seconds: float = 30.0  # the client's default
    optimize_scope: Literal["table", "cold-partitions"] = "table"
    assign: Assign = round_robin
    definite_errors: frozenset[str] = field(default_factory=frozenset)  # error classes that guarantee no commit
    # Builds a writer's apply function, (conn, scenario, arm, writer id) -> Apply; None: the baseline writes.
    writes: Callable | None = None


BASELINE = Arm("baseline")


def op_streams(scenario: Scenario, assign: Assign = round_robin) -> list[list[OpSpec]]:
    """Each writer's ops, in the order it sends them; `seq` counts from 1 within each writer."""
    n = scenario.writers
    if scenario.contention == "low":
        # Each writer owns its keys; the assignment has nothing to decide.
        streams = []
        for writer in range(n):
            rng = random.Random(f"{scenario.seed}:{writer}")  # noqa: S311  # a seeded workload, not a secret
            keys = range(KEYS_PER_WRITER_LOW * writer, KEYS_PER_WRITER_LOW * (writer + 1))
            streams.append([(rng.choice(keys), rng.choice(DELTAS)) for _ in range(scenario.ops_per_writer)])
    else:
        rng = random.Random(str(scenario.seed))  # noqa: S311  # a seeded workload, not a secret
        keys = scenario.seeded_keys()
        streams = [[] for _ in range(n)]
        for index in range(scenario.ops_per_writer * n):
            key, delta = rng.choice(keys), rng.choice(DELTAS)
            writer = assign(index, key, n)
            if not 0 <= writer < n:
                raise ValueError(f"assignment sent op {index} to writer {writer} of {n}")
            streams[writer].append((key, delta))
    return [[OpSpec(seq, key, delta) for seq, (key, delta) in enumerate(stream, 1)] for stream in streams]
