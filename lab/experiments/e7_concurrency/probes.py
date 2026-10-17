"""Probes for the questions the research doc answers by measurement; lab stack only.

    cd lab/experiments && uv run --project .. python -m e7_concurrency.probes [q1 q2 ...] [--out DIR]

Each probe recreates the experiment's tables, does one controlled thing, and writes one JSON evidence record
(outcomes, error classes with a sample message, and the oracle's verdict on the final state) to
DIR/probes-<utc>.jsonl (default $LAB_RESULTS_DIR/e7_concurrency, else out/lab-e7). Nothing secret is written: no
credential appears in a statement, an error message or a record.
"""

import argparse
import json
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import requests

from . import sql
from .oplog import Op, Outcome
from .oracle import CounterRow, EventRow, grade
from .runner import ADMIN, lab_connector, query
from .scenario import DAY0, Arm
from .writers import Attempted, execute_once

ONE_ATTEMPT = Arm("probe", client_max_attempts=1)
REPO = Path(__file__).resolve().parents[3]


def _fresh(admin, keys: int = 1) -> None:
    for statement in sql.recreate_tables():
        query(admin, statement)
    query(admin, sql.seed_counters(range(keys)))


def _together(calls: list[Callable[[], Attempted]]) -> list[Attempted]:
    """Runs the calls in threads released at one barrier; results in call order."""
    barrier, results = threading.Barrier(len(calls)), [None] * len(calls)

    def run(i: int) -> None:
        barrier.wait()
        results[i] = calls[i]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def _op(writer: str, seq: int, table: str, key: int, result: Attempted) -> Op:
    return Op(writer, seq, table, key, 1, result.outcome, attempts=result.attempts, error=result.error)


def _summary(results: list[Attempted]) -> dict:
    outcomes = Counter(f"{r.outcome.value}:{r.error}" if r.error else r.outcome.value for r in results)
    samples = {}
    for r in results:
        if r.error and r.error not in samples:
            samples[r.error] = r.detail
    return {"outcomes": dict(outcomes), "error_samples": samples}


def _counters(admin) -> list[CounterRow]:
    return [CounterRow(k, v, tuple(a)) for k, v, a in query(admin, sql.READ_COUNTERS)]


def q1_same_key_merges(trials: int = 20) -> dict:
    """Two MERGEs on the same key at the same moment: does Trino detect the conflict, and how does it say so?"""
    admin = lab_connector(ADMIN)
    _fresh(admin)
    conns = [lab_connector(ONE_ATTEMPT) for _ in range(2)]
    ops, results = [], []
    for trial in range(1, trials + 1):
        pair = _together(
            [
                (lambda c=c, w=w, t=trial: execute_once(c, sql.add_to_counter(f"p{w}:{t}", 0, 1), frozenset()))
                for w, c in enumerate(conns)
            ]
        )
        results += pair
        ops += [_op(f"p{w}", trial, "counters", 0, r) for w, r in enumerate(pair)]
    verdict = grade(ops, [], _counters(admin), range(1))
    return {"trials": trials, **_summary(results), "verdict": verdict.counts()}


def q2_optimize_vs_merge(trials: int = 10, files: int = 5) -> dict:
    """OPTIMIZE on counters racing a MERGE on a key: does one fail, or does a deleted row come back?"""
    admin = lab_connector(ADMIN)
    _fresh(admin)
    merger, optimizer = lab_connector(ONE_ATTEMPT), lab_connector(ONE_ATTEMPT)
    ops, merges, optimizes, seq = [], [], [], 0
    for _ in range(trials):
        for _ in range(files):  # small files and position deletes for OPTIMIZE to rewrite
            seq += 1
            r = execute_once(admin, sql.add_to_counter(f"setup:{seq}", 0, 1), frozenset())
            ops.append(_op("setup", seq, "counters", 0, r))
        seq += 1
        merge, opt = _together(
            [
                lambda s=seq: execute_once(merger, sql.add_to_counter(f"race:{s}", 0, 1), frozenset()),
                lambda: execute_once(optimizer, sql.optimize(sql.COUNTERS), frozenset()),
            ]
        )
        merges.append(merge)
        optimizes.append(opt)
        ops.append(_op("race", seq, "counters", 0, merge))
    verdict = grade(ops, [], _counters(admin), range(1))
    return {"trials": trials, "merge": _summary(merges), "optimize": _summary(optimizes), "verdict": verdict.counts()}


def q3_commit_retries(rounds: int = 10, writers: int = 4) -> dict:
    """Concurrent appends with the default commit retries, then with `commit.retry.num-retries` = 0 set through
    `extra_properties`: if failures rise, Trino honours the table property."""
    admin = lab_connector(ADMIN)
    _fresh(admin)
    conns = [lab_connector(ONE_ATTEMPT) for _ in range(writers)]
    evidence: dict = {"rounds": rounds, "writers": writers}
    seq = 0
    for setting in ("default", "retries-0"):
        if setting == "retries-0":
            change = execute_once(
                admin,
                f"ALTER TABLE {sql.EVENTS} SET PROPERTIES extra_properties = "
                "MAP(ARRAY['commit.retry.num-retries'], ARRAY['0'])",
                frozenset(),
            )
            evidence["set_property"] = {"outcome": change.outcome.value, "error": change.error, "detail": change.detail}
            if change.outcome is not Outcome.ACKED:
                break
        results = []
        for _ in range(rounds):
            seq += 1
            results += _together(
                [
                    (lambda c=c, w=w, s=seq: execute_once(c, sql.insert_event(f"a{w}", s, w, 1, DAY0), frozenset()))
                    for w, c in enumerate(conns)
                ]
            )
        evidence[setting] = _summary(results)
    evidence["properties"] = [list(r) for r in query(admin, 'SELECT * FROM lab_exp.lab_e7."events$properties"')]
    return evidence


def q4_snapshot_query_id() -> dict:
    """Does a snapshot summary carry the Trino query id of the statement that committed it?"""
    admin = lab_connector(ADMIN)
    _fresh(admin)
    result = execute_once(admin, sql.insert_event("q4", 1, 0, 1, DAY0), frozenset())
    rows = query(admin, sql.snapshots(sql.EVENTS))
    summary = rows[-1][2] or {}
    return {
        "insert": result.outcome.value,
        "summary_keys": sorted(summary),
        "query_id_matches": bool(result.query_ids) and summary.get("trino_query_id") == result.query_ids[0],
    }


def _query_state(admin, query_id: str) -> str | None:
    if not re.fullmatch(r"[0-9a-z_]+", query_id):
        raise ValueError(f"not a Trino query id: {query_id!r}")
    rows = query(admin, f"SELECT state FROM system.runtime.queries WHERE query_id = '{query_id}'")  # noqa: S608
    return rows[0][0] if rows else None


def _rows_for(admin, writer: str) -> int:
    return query(admin, f"SELECT count(*) FROM {sql.EVENTS} WHERE writer_id = '{writer}'")[0][0]  # noqa: S608


def q5_write_without_its_client(wait_seconds: float = 20.0) -> dict:
    """A write whose client goes away: (a) after the POST, before any poll; (b) after the first poll. Does Trino run
    it anyway?"""
    from lab.trino_client import Settings

    admin = lab_connector(ADMIN)
    _fresh(admin)
    settings = Settings.from_env()
    auth = ("harness", settings.password("harness"))
    evidence = {}
    for case, polls in (("post-only", 0), ("post-and-one-poll", 1)):
        writer = "q5a" if polls == 0 else "q5b"
        response = requests.post(
            f"{settings.url.geturl()}/v1/statement",
            data=sql.insert_event(writer, 1, 0, 1, DAY0).encode(),
            headers={"X-Trino-User": "harness"},
            auth=auth,
            verify=str(settings.ca),
            timeout=30,
        )
        body = response.json()
        if polls and body.get("nextUri"):
            requests.get(body["nextUri"], auth=auth, verify=str(settings.ca), timeout=30)
        time.sleep(wait_seconds)
        evidence[case] = {"state": _query_state(admin, body["id"]), "rows": _rows_for(admin, writer)}
    evidence["wait_seconds"] = wait_seconds
    return evidence


class LoseFirstStatementResponse(requests.Session):
    """Sends the first `POST /v1/statement` for real, then raises a connection error instead of returning its
    response: the server has the query, the client thinks the request failed."""

    def __init__(self) -> None:
        super().__init__()
        self.lost_query_id: str | None = None

    def post(self, url, *args, **kwargs):
        response = super().post(url, *args, **kwargs)
        if self.lost_query_id is None and url.endswith("/v1/statement"):
            self.lost_query_id = response.json().get("id")
            raise requests.exceptions.ConnectionError("probe: the response to the first statement POST was lost")
        return response


def q7_client_resubmission(wait_seconds: float = 20.0) -> dict:
    """The trino client's own retry after a lost statement response: one write, or two?"""
    from lab.trino_client import Settings, connect

    admin = lab_connector(ADMIN)
    _fresh(admin)
    settings = Settings.from_env()
    evidence = {}
    for case, attempts in (("client-default-3-attempts", 3), ("one-attempt", 1)):
        writer = f"q7-{attempts}"
        session = LoseFirstStatementResponse()
        session.verify = str(settings.ca)
        conn = connect("harness", settings, http_session=session, max_attempts=attempts)
        result = execute_once(conn, sql.insert_event(writer, 1, 0, 1, DAY0), frozenset())
        time.sleep(wait_seconds)
        lost = session.lost_query_id
        evidence[case] = {
            "client": result.outcome.value,
            "client_error": result.error,
            "lost_query_state": _query_state(admin, lost) if lost else None,
            "rows": _rows_for(admin, writer),
        }
    events = [EventRow(*r) for r in query(admin, sql.READ_EVENTS)]
    evidence["rows_total"] = len(events)
    evidence["wait_seconds"] = wait_seconds
    return evidence


PROBES = {
    "q1": q1_same_key_merges,
    "q2": q2_optimize_vs_merge,
    "q3": q3_commit_retries,
    "q4": q4_snapshot_query_id,
    "q5": q5_write_without_its_client,
    "q7": q7_client_resubmission,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("probes", nargs="*", choices=[*PROBES, []], default=list(PROBES))
    results = os.environ.get("LAB_RESULTS_DIR")
    default_out = Path(results) / "e7_concurrency" if results else REPO / "out/lab-e7"
    parser.add_argument("--out", type=Path, default=default_out)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"probes-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl"
    with path.open("a", encoding="utf-8") as out:
        for name in args.probes:
            started = time.time()
            record = {
                "probe": name,
                "started_at": started,
                **PROBES[name](),
                "seconds": round(time.time() - started, 1),
            }
            out.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            out.flush()
            print(f"{name}: done in {record['seconds']} s")
    print(f"evidence: {path}")


if __name__ == "__main__":
    main()
