"""Writer and maintenance loops: one process each, every op logged before it is sent and after its outcome is known.

The baseline writes are the default ones: one statement per op, no retry by the application, the trino client and
Trino left at their defaults. A method plugs in through `Arm.writes`, a factory with the same signature as
`baseline_writes`.
"""

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trino.dbapi import Connection, Cursor

from . import sql
from .classify import classify
from .oplog import OpLogWriter, Outcome
from .scenario import Arm, OpSpec, Scenario


@dataclass(frozen=True)
class Attempted:
    """What the client saw of one op, over all its attempts."""

    outcome: Outcome
    attempts: int
    error: str | None = None
    detail: str | None = None
    query_ids: tuple[str, ...] = ()
    settled: tuple[str, ...] = ()  # what each ambiguous attempt settled to (lakeops.writes.WriteResult.settled)


Apply = Callable[[str, OpSpec, date], Attempted]  # (op id, op, event day) -> what the client saw


def execute_once(conn: Connection, statement: str, definite: frozenset[str]) -> Attempted:
    """One statement; reading its result is what waits for a write to finish."""
    cur = conn.cursor()
    try:
        cur.execute(statement)
        cur.fetchall()
    except Exception as error:
        outcome, cls, detail = classify(error, definite)
        return Attempted(outcome, 1, cls, detail, _query_ids(cur))
    return Attempted(Outcome.ACKED, 1, query_ids=_query_ids(cur))


def _query_ids(cur: Cursor) -> tuple[str, ...]:
    return (cur.query_id,) if getattr(cur, "query_id", None) else ()


def baseline_writes(conn: Connection, scenario: Scenario, arm: Arm, writer: str) -> Apply:
    if scenario.workload == "w1":

        def insert(_oid: str, op: OpSpec, day: date) -> Attempted:
            return execute_once(conn, sql.insert_event(writer, op.seq, op.key, op.delta, day), arm.definite_errors)

        return insert

    def add(oid: str, op: OpSpec, _day: date) -> Attempted:
        statement = sql.add_to_counter(oid, op.key, op.delta, ledger=scenario.ledger)
        return execute_once(conn, statement, arm.definite_errors)

    return add


def run_ops(
    ops: Sequence[OpSpec],
    *,
    table: str,
    log: OpLogWriter,
    apply: Apply,
    event_day: Callable[[float], date],
    start: float,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Sends ops until the deadline; an op already sent always gets its outcome logged. Returns how many were sent."""
    sent = 0
    for op in ops:
        sent_at = clock()
        if sent_at >= deadline:
            break
        oid = log.invoke(op.seq, table, op.key, op.delta)
        result = apply(oid, op, event_day(sent_at - start))
        log.complete(
            oid,
            result.outcome,
            attempts=result.attempts,
            error=result.error,
            detail=result.detail,
            latency_ms=round((clock() - sent_at) * 1000, 3),
            query_ids=result.query_ids,
            settled=result.settled,
        )
        sent += 1
    return sent


def maintenance_statement(scenario: Scenario, arm: Arm) -> Callable[[float], str]:
    """The OPTIMIZE to run `elapsed` seconds into the write phase: the whole table, or the event days already past."""
    table = sql.EVENTS if scenario.workload == "w1" else sql.COUNTERS
    if arm.optimize_scope == "table" or scenario.workload != "w1":
        return lambda _elapsed: sql.optimize(table)
    # Writers stamp the current day (writer 0's at low contention): compact only the days before it.
    return lambda elapsed: sql.optimize(table, before_day=scenario.event_day(0, elapsed))


def run_maintenance(
    conn: Connection,
    *,
    statement: Callable[[float], str],
    log_path: Path,
    every: float,
    definite: frozenset[str],
    start: float,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Runs a maintenance statement every `every` seconds until the deadline, logging each run; returns the count."""
    runs = 0
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        while (now := clock()) < deadline:
            elapsed = now - start
            text = statement(elapsed)
            result = execute_once(conn, text, definite)
            record = {
                "type": "maintenance",
                "elapsed_s": round(elapsed, 3),
                "sql": text,
                "outcome": result.outcome.value,
                "error": result.error,
                "detail": result.detail,
                "latency_ms": round((clock() - now) * 1000, 3),
                "query_ids": list(result.query_ids),
            }
            log.write(json.dumps(record, separators=(",", ":")) + "\n")
            runs += 1
            pause = min(start + runs * every, deadline) - clock()
            if pause > 0:
                sleep(pause)
    return runs
