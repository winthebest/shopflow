"""The method arm: writes from lakeops.writes, ops routed to writers by key, OPTIMIZE only on past days.

- Every write is idempotent: events with `insert_unless_present` on (writer_id, seq, key, event_day), counter adds
  with `update_once` on the ledger. A retry of a write that did commit changes nothing.
- Those forms rather than MERGE: on the lab, Trino checked every MERGE against all concurrent commits to the table
  (conflict filter `true`, probes q1 and q1b), while an UPDATE of one key conflicted only within its partition and an
  INSERT (an append) not at all.
- `lakeops.writes.write` runs one attempt at a time and settles an ambiguous attempt before the next.
- One HTTP attempt per request, so the trino client never re-sends a statement on its own.
- Two writers never update one key (`by_key`), and OPTIMIZE leaves the day being written alone.
"""

from datetime import date

from trino.dbapi import Connection

from lakeops.writes import RetryPolicy, WriteResult, insert_row, insert_unless_present, update_once, write

from . import sql
from .oplog import Outcome
from .scenario import Arm, OpSpec, Scenario
from .writers import Apply, Attempted

_OUTCOME = {"committed": Outcome.ACKED, "failed": Outcome.FAILED, "unknown": Outcome.UNKNOWN}


def by_key(_index: int, key: int, writers: int) -> int:
    return key % writers


def attempted(result: WriteResult) -> Attempted:
    return Attempted(
        _OUTCOME[result.state], result.attempts, result.error, result.detail, result.query_ids, result.settled
    )


def method_writes(conn: Connection, scenario: Scenario, arm: Arm, writer: str) -> Apply:
    if not scenario.ledger:
        raise ValueError("the method's counter adds are idempotent only through the ledger")
    policy = _policy(arm)
    if scenario.workload == "w1":

        def insert(_oid: str, op: OpSpec, day: date) -> Attempted:
            statement = insert_unless_present(
                sql.EVENTS,
                key={"writer_id": writer, "seq": op.seq, "key": op.key, "event_day": day},
                values={"delta": op.delta},
            )
            return attempted(write(conn, statement, policy))

        return insert

    def add(oid: str, op: OpSpec, _day: date) -> Attempted:
        statement = update_once(
            sql.COUNTERS,
            key_column="key",
            key=op.key,
            value_column="value",
            delta=op.delta,
            ledger_column="applied",
            op_id=oid,
        )
        return attempted(write(conn, statement, policy))

    return add


def _policy(arm: Arm) -> RetryPolicy:
    # A statement whose POST got no answer never ran on Trino 483 (probes q5, q7): retrying it cannot double a write.
    return RetryPolicy(definite=arm.definite_errors, retry_without_query_id=True)


def _event(writer: str, op: OpSpec, day: date) -> tuple[dict, dict]:
    return {"writer_id": writer, "seq": op.seq, "key": op.key, "event_day": day}, {"delta": op.delta}


def resolving_writes(conn: Connection, scenario: Scenario, arm: Arm, writer: str) -> Apply:
    """The method, with an ambiguous attempt resolved from the table's snapshots (probe q4: Trino's query id is in
    every snapshot it commits). An event's first attempt is then a plain INSERT, with no read before it; only its
    retries use the idempotent form. Counter adds keep `update_once`, which costs no extra read."""
    if not scenario.ledger:
        raise ValueError("the method's counter adds are idempotent only through the ledger")
    policy = _policy(arm)
    if scenario.workload == "w1":

        def insert(_oid: str, op: OpSpec, day: date) -> Attempted:
            key, values = _event(writer, op, day)
            result = write(
                conn,
                insert_row(sql.EVENTS, key | values),
                policy,
                retry_statement=insert_unless_present(sql.EVENTS, key=key, values=values),
                table=sql.EVENTS,
            )
            return attempted(result)

        return insert

    def add(oid: str, op: OpSpec, _day: date) -> Attempted:
        statement = update_once(
            sql.COUNTERS,
            key_column="key",
            key=op.key,
            value_column="value",
            delta=op.delta,
            ledger_column="applied",
            op_id=oid,
        )
        return attempted(write(conn, statement, policy, table=sql.COUNTERS))

    return add


METHOD = Arm(
    "method",
    client_max_attempts=1,
    optimize_scope="cold-partitions",
    assign=by_key,
    writes=method_writes,
)
METHOD_RESOLVE = Arm(
    "method-resolve",
    client_max_attempts=1,
    optimize_scope="cold-partitions",
    assign=by_key,
    writes=resolving_writes,
)
