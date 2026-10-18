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

from lakeops.writes import RetryPolicy, WriteResult, insert_unless_present, update_once, write

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
    policy = RetryPolicy(definite=arm.definite_errors)
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


METHOD = Arm(
    "method",
    client_max_attempts=1,
    optimize_scope="cold-partitions",
    assign=by_key,
    writes=method_writes,
)
