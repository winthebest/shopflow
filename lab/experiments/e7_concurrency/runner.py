"""One run of a scenario under an arm: fresh tables, writer processes started together, then the grade.

- `prepare`: recreate the tables (and seed the counters), and check that they are stored in the lab.
- `launch`: the writers (and the maintenance loop) start at one barrier. After the write phase and the drain, any
  process still running is killed, and its open op stays unknown.
- `collect`: read the op logs and the final tables, grade them, and build the results row.
"""

import json
import multiprocessing
import os
import signal
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from trino.dbapi import Connection

from . import sql
from .oplog import Op, OpLogWriter, Outcome, read_oplog
from .oracle import CounterRow, EventRow, acked_commits, correct_commit_throughput, grade, grade_bounds
from .scenario import Arm, OpSpec, Scenario, op_streams
from .writers import baseline_writes, maintenance_statement, run_maintenance, run_ops

EXPERIMENT = "e7_concurrency"
BARRIER_TIMEOUT_SECONDS = 120.0  # every process connects (and passes the lab guard) before the write phase starts
ADMIN = Arm("admin", client_max_attempts=1, request_timeout_seconds=300.0)  # setup and read-back, never retried

Connector = Callable[[Arm], Connection]


def lab_connector(arm: Arm) -> Connection:
    """A `harness` connection to the lab stack, after the lab guard, with the arm's client settings."""
    from lab.guard import guarded_connect  # the lab package, imported only where a real run needs it

    conn = guarded_connect("harness")
    # lab.trino_client.connect takes no client options; the connection reads both per request.
    if arm.client_max_attempts is not None:
        conn.max_attempts = arm.client_max_attempts
    conn.request_timeout = arm.request_timeout_seconds
    return conn


def lab_check_table(conn: Connection, table: str) -> None:
    from lab.guard import check_table

    check_table(conn, table)


def query(conn: Connection, statement: str) -> list:
    cur = conn.cursor()
    cur.execute(statement)
    return cur.fetchall()


def prepare(conn: Connection, scenario: Scenario, check_table: Callable[[Connection, str], None]) -> None:
    for statement in sql.recreate_tables(ledger=scenario.ledger):
        query(conn, statement)
    for table in (sql.EVENTS, sql.COUNTERS):
        check_table(conn, table)
    if scenario.workload == "w2":
        query(conn, sql.seed_counters(scenario.seeded_keys(), ledger=scenario.ledger))


@dataclass(frozen=True)
class Kill:
    """Fault injection: SIGKILL writer `writer` `at` seconds into the write phase."""

    writer: int
    at: float


@dataclass(frozen=True)
class Launched:
    seconds: float  # from the barrier until the last process ended
    killed: tuple[str, ...] = ()  # by fault injection
    stragglers: tuple[str, ...] = ()  # still running after the drain, then killed
    crashed: tuple[str, ...] = ()  # ended with an error of their own
    exit_codes: dict[str, int | None] = field(default_factory=dict)


def writer_name(scenario: Scenario, index: int) -> str:
    return f"{scenario.workload}-{index}"


def _writer_main(
    index: int, ops: list[OpSpec], scenario: Scenario, arm: Arm, run_dir: Path, barrier, connector: Connector
) -> None:
    name = writer_name(scenario, index)
    conn = connector(arm)
    apply = (arm.writes or baseline_writes)(conn, scenario, arm, name)
    with OpLogWriter(run_dir / f"{name}.jsonl", name) as log:
        barrier.wait(BARRIER_TIMEOUT_SECONDS)
        start = time.monotonic()
        run_ops(
            ops,
            table=scenario.table,
            log=log,
            apply=apply,
            event_day=partial(scenario.event_day, index),
            start=start,
            deadline=start + scenario.seconds,
        )


def _maintenance_main(scenario: Scenario, arm: Arm, run_dir: Path, barrier, connector: Connector) -> None:
    conn = connector(arm)
    barrier.wait(BARRIER_TIMEOUT_SECONDS)
    start = time.monotonic()
    run_maintenance(
        conn,
        statement=maintenance_statement(scenario, arm),
        log_path=run_dir / "maintenance.jsonl",
        every=scenario.optimize_every_seconds,
        definite=arm.definite_errors,
        start=start,
        deadline=start + scenario.seconds,
    )


def launch(scenario: Scenario, arm: Arm, run_dir: Path, connector: Connector, kill: Kill | None = None) -> Launched:
    ctx = multiprocessing.get_context("spawn")
    streams = op_streams(scenario, arm.assign)
    maintenance = scenario.maintenance == "optimize"
    barrier = ctx.Barrier(len(streams) + maintenance + 1)
    procs = {
        writer_name(scenario, i): ctx.Process(
            target=_writer_main, args=(i, ops, scenario, arm, run_dir, barrier, connector), daemon=True
        )
        for i, ops in enumerate(streams)
    }
    if maintenance:
        procs["maintenance"] = ctx.Process(
            target=_maintenance_main, args=(scenario, arm, run_dir, barrier, connector), daemon=True
        )
    for proc in procs.values():
        proc.start()
    try:
        barrier.wait(BARRIER_TIMEOUT_SECONDS)
    except Exception:
        for proc in procs.values():
            proc.kill()
            proc.join()
        raise
    start = time.monotonic()

    killed = []
    if kill is not None:
        time.sleep(max(0.0, start + kill.at - time.monotonic()))
        victim = procs[writer_name(scenario, kill.writer)]
        if victim.is_alive():
            os.kill(victim.pid, signal.SIGKILL)
            killed.append(writer_name(scenario, kill.writer))

    end_by = start + scenario.seconds + scenario.drain_seconds
    for proc in procs.values():
        proc.join(max(0.0, end_by - time.monotonic()))
    seconds = time.monotonic() - start
    stragglers = [name for name, proc in procs.items() if proc.is_alive()]
    for name in stragglers:
        procs[name].kill()
        procs[name].join()
    codes = {name: proc.exitcode for name, proc in procs.items()}
    crashed = [name for name, code in codes.items() if code not in (0, None) and name not in killed + stragglers]
    return Launched(seconds, tuple(killed), tuple(stragglers), tuple(crashed), codes)


def _percentile(values: list[float], q: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def snapshot_stats(rows: list, ops: list[Op]) -> dict:
    """From `$snapshots`: write amplification (bytes of files added ÷ bytes in the final snapshot), the operations,
    and the share of acked ops whose Trino query id appears in a snapshot summary."""
    summaries = [summary or {} for _, _, summary in rows]
    added = sum(int(s.get("added-files-size", 0)) for s in summaries)
    final = int(summaries[-1].get("total-files-size", 0)) if summaries else 0
    query_ids = {s["trino_query_id"] for s in summaries if "trino_query_id" in s}
    acked = [op for op in ops if op.outcome is Outcome.ACKED]
    found = sum(any(q in query_ids for q in op.query_ids) for op in acked)
    return {
        "snapshots": len(rows),
        "snapshot_operations": json.dumps(dict(Counter(operation for _, operation, _ in rows)), sort_keys=True),
        "write_amplification": round(added / final, 4) if final else None,
        "acked_with_snapshot_query_id": round(found / len(acked), 4) if acked else None,
    }


def maintenance_stats(path: Path) -> dict:
    if not path.exists():
        return {"maintenance_runs": 0, "maintenance_outcomes": "{}"}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    outcomes = Counter(f"{r['outcome']}:{r['error']}" if r["error"] else r["outcome"] for r in records)
    return {"maintenance_runs": len(records), "maintenance_outcomes": json.dumps(dict(outcomes), sort_keys=True)}


def collect(conn: Connection, scenario: Scenario, arm: Arm, run_dir: Path, launched: Launched) -> dict:
    ops = read_oplog(sorted(run_dir.glob(f"{scenario.workload}-*.jsonl")))
    events = [EventRow(*row) for row in query(conn, sql.READ_EVENTS)]
    seeded = scenario.seeded_keys() if scenario.workload == "w2" else range(0)
    if scenario.ledger:
        counters = [CounterRow(key, value, tuple(applied)) for key, value, applied in query(conn, sql.READ_COUNTERS)]
        verdict = grade(ops, events, counters, seeded)
        valid, grading = verdict.valid, verdict.counts()
        cct = correct_commit_throughput(verdict, ops, launched.seconds)
        max_ledger = max((len(c.applied) for c in counters), default=0)
    else:
        bounds = grade_bounds(ops, query(conn, sql.READ_COUNTERS_NO_LEDGER), seeded)
        valid, max_ledger = bounds.valid, None
        cct = acked_commits(ops) / launched.seconds if valid else None
        grading = {"bounds_below": bounds.below, "bounds_above": bounds.above, "wrong_final_rows": bounds.wrong_rows}
    table = sql.EVENTS if scenario.workload == "w1" else sql.COUNTERS
    acked = [op for op in ops if op.outcome is Outcome.ACKED]
    latencies = sorted(op.latency_ms for op in acked if op.latency_ms is not None)
    return {
        "experiment": EXPERIMENT,
        "run_id": run_dir.name,
        "scenario": scenario.name,
        "workload": scenario.workload,
        "writers": scenario.writers,
        "contention": scenario.contention,
        "seed": scenario.seed,
        "write_phase_seconds": scenario.seconds,
        "ledger": scenario.ledger,
        "maintenance": scenario.maintenance,
        "arm": arm.name,
        "seconds": round(launched.seconds, 3),
        "ops": len(ops),
        "acked_commits": acked_commits(ops),
        "valid": valid,
        "cct": round(cct, 4) if cct is not None else None,
        **grading,
        "attempts": sum(op.attempts for op in ops),
        "retries": sum(max(0, op.attempts - 1) for op in ops),
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "abort_rate": round(sum(op.outcome is Outcome.FAILED for op in ops) / len(ops), 4) if ops else None,
        "unknown_rate": round(sum(op.outcome is Outcome.UNKNOWN for op in ops) / len(ops), 4) if ops else None,
        "error_classes": json.dumps(dict(Counter(op.error for op in ops if op.error)), sort_keys=True),
        "max_ledger": max_ledger,
        **snapshot_stats(query(conn, sql.snapshots(table)), ops),
        **maintenance_stats(run_dir / "maintenance.jsonl"),
        "killed": json.dumps(list(launched.killed)),
        "stragglers": json.dumps(list(launched.stragglers)),
        "crashed": json.dumps(list(launched.crashed)),
    }


def run(
    scenario: Scenario,
    arm: Arm,
    base_dir: Path,
    *,
    connector: Connector = lab_connector,
    check_table: Callable[[Connection, str], None] = lab_check_table,
    kill: Kill | None = None,
) -> dict:
    """Runs once and returns the results row; the op logs and the row are kept in `base_dir/<run id>/`."""
    run_id = f"{scenario.name}-{arm.name}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = base_dir / run_id
    run_dir.mkdir(parents=True)
    admin = connector(ADMIN)
    prepare(admin, scenario, check_table)
    launched = launch(scenario, arm, run_dir, connector, kill)
    row = collect(admin, scenario, arm, run_dir, launched)
    (run_dir / "result.json").write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return row
