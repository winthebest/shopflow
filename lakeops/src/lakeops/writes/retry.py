"""The retry loop: one attempt at a time, backoff with full jitter, and an ambiguous attempt settled before the next.

An attempt that fails without a user error may still have committed (a timeout, a lost connection, a commit whose
state Iceberg could not confirm). Before trying again, `write` cancels that attempt and polls
`system.runtime.queries` until Trino reports it FINISHED (it did commit: stop) or FAILED (try again). If the state
cannot be read within the policy's time, the write ends unknown rather than risk two attempts running at once.
"""

import contextlib
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import requests
from trino import exceptions as trino_errors
from trino.dbapi import Connection, Cursor

from .errors import RETRYABLE_USER_ERRORS, error_class, is_fatal

_QUERY_ID = re.compile(r"[0-9a-z_]+")
TERMINAL = ("FINISHED", "FAILED")


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    base_seconds: float = 0.1  # backoff before attempt n+1: uniform in [0, min(cap, base * 2^(n-1))]
    cap_seconds: float = 3.0
    settle_seconds: float = 60.0  # how long to wait for an ambiguous attempt to become terminal
    poll_seconds: float = 0.5
    # Error classes that guarantee nothing committed; with none listed, a write that never succeeded ends unknown.
    definite: frozenset[str] = field(default_factory=frozenset)
    retryable_user_errors: frozenset[str] = RETRYABLE_USER_ERRORS
    # An attempt that failed before Trino gave it a query id cannot be settled. True only once it is shown that such
    # a statement never runs on the server.
    retry_without_query_id: bool = False


@dataclass(frozen=True)
class WriteResult:
    state: Literal["committed", "failed", "unknown"]
    attempts: int
    error: str | None = None  # class of the last error
    detail: str | None = None
    query_ids: tuple[str, ...] = ()
    # What each ambiguous attempt settled to: FINISHED, FAILED, no-query-id, or unsettled:<last seen> where the last
    # seen is a state (still RUNNING…), not-found (gone from system.runtime.queries, or never there) or unreadable.
    settled: tuple[str, ...] = ()


def _query_state(conn: Connection, query_id: str) -> str | None:
    if not _QUERY_ID.fullmatch(query_id):
        raise ValueError(f"not a Trino query id: {query_id!r}")
    cur = conn.cursor()
    cur.execute(f"SELECT state FROM system.runtime.queries WHERE query_id = '{query_id}'")  # noqa: S608
    rows = cur.fetchall()
    return rows[0][0] if rows else None


def settle(
    conn: Connection,
    cur: Cursor,
    query_id: str,
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str | None, str]:
    """Cancels the attempt and waits until it is FINISHED or FAILED. Returns that state (None if it is not known in
    time) and the last thing seen: a state, `not-found` or `unreadable`."""
    # A DELETE of the query's next URI; nothing to do if the query is already over. The state below decides: a failed
    # cancel only means waiting for the query to end by itself.
    with contextlib.suppress(trino_errors.Error, requests.exceptions.RequestException):
        cur.cancel()
    deadline = clock() + policy.settle_seconds
    while True:
        try:
            state = _query_state(conn, query_id)
            seen = state or "not-found"
        except (trino_errors.Error, requests.exceptions.RequestException):
            state, seen = None, "unreadable"  # poll again until the deadline
        if state in TERMINAL:
            return state, seen
        if clock() >= deadline:
            return None, seen
        sleep(policy.poll_seconds)


def write(
    conn: Connection,
    statement: str,
    policy: RetryPolicy = RetryPolicy(),  # noqa: B008  # frozen, so one shared default is safe
    *,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    clock: Callable[[], float] = time.monotonic,
) -> WriteResult:
    """Runs an idempotent statement until it commits, fails for good, or its fate cannot be known."""
    query_ids: list[str] = []
    settled: list[str] = []
    ambiguous = False
    error = detail = None
    for attempt in range(1, policy.attempts + 1):
        cur = conn.cursor()
        try:
            cur.execute(statement)
            cur.fetchall()  # a write is done only when its result has been read
        except Exception as exc:
            query_id = cur.query_id
            if query_id:
                query_ids.append(query_id)
            error, detail = error_class(exc), str(exc)[:500]
            if is_fatal(exc, policy.retryable_user_errors):
                return WriteResult("failed", attempt, error, detail, tuple(query_ids), tuple(settled))
            if error in policy.definite:
                state = "FAILED"
            elif query_id:
                state, seen = settle(conn, cur, query_id, policy, sleep=sleep, clock=clock)
                settled.append(state or f"unsettled:{seen}")
            else:
                state = "FAILED" if policy.retry_without_query_id else None
                settled.append("no-query-id")
            if state == "FINISHED":  # it committed after all: the error came after the commit
                return WriteResult("committed", attempt, error, detail, tuple(query_ids), tuple(settled))
            if state is None:
                return WriteResult("unknown", attempt, error, detail, tuple(query_ids), tuple(settled))
            ambiguous = ambiguous or error not in policy.definite
            if attempt < policy.attempts:
                sleep(jitter() * min(policy.cap_seconds, policy.base_seconds * 2 ** (attempt - 1)))
            continue
        if cur.query_id:
            query_ids.append(cur.query_id)
        return WriteResult("committed", attempt, error, detail, tuple(query_ids), tuple(settled))
    return WriteResult(
        "unknown" if ambiguous else "failed", policy.attempts, error, detail, tuple(query_ids), tuple(settled)
    )
