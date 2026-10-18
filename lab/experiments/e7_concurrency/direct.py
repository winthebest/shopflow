"""Writers that commit through the REST catalog with PyIceberg, without Trino, as the Kafka Connect sink does.

- W2-py (`pyiceberg_writes`): a counter add as a client-side read-modify-write. Read the key's row, add the delta and
  the op id, and `upsert` the row back. PyIceberg retries a commit that lost a race (`commit.retry.num-retries`,
  4 by default) and checks the retry against the table's isolation level. Whether a racing add can still be lost is
  question 6.
- The sink-like appender (`run_appender`): small batches of events, one `append` (one commit) each. The events of a
  batch share a group, so the oracle checks that a batch landed whole or not at all.

They run inside the lab network (`lab/stack.sh py exp -- …`), where `lab.iceberg.rest_catalog()` builds the catalog
from the zone's credentials; no code here touches a credential.
"""

import time
from collections.abc import Callable
from datetime import date
from typing import Any

import pyarrow as pa

from . import sql
from .classify import classify
from .oplog import OpLogWriter, Outcome, op_id
from .scenario import DELTAS, Arm, OpSpec, Scenario
from .writers import Apply, Attempted


def lab_catalog() -> Any:
    from lab.iceberg import rest_catalog  # the lab's helper: zone credentials from the container's environment

    return rest_catalog()


def load(table: str, catalog_factory: Callable[[], Any] | None = None) -> Any:
    """`lab_exp.lab_e7.counters` → the PyIceberg table `lab_e7.counters` in the zone's warehouse."""
    catalog, schema, name = table.split(".")
    if catalog != sql.CATALOG:
        raise ValueError(f"direct writers write only to {sql.CATALOG}")
    return (catalog_factory or lab_catalog)().load_table(f"{schema}.{name}")


def pyiceberg_writes(_conn: object, scenario: Scenario, arm: Arm, _writer: str) -> Apply:
    """W2-py. The Trino connection the runner passes is not used for writing (its guard checks ran on connect)."""
    if scenario.workload != "w2" or not scenario.ledger:
        raise ValueError("W2-py adds to counters with the ledger")
    table = load(sql.COUNTERS)

    def add(oid: str, op: OpSpec, _day: date) -> Attempted:
        try:
            table.refresh()
            rows = table.scan(row_filter=f"key = {op.key}").to_arrow().to_pylist()
            if len(rows) != 1:
                raise LookupError(f"key {op.key} has {len(rows)} rows")
            row = rows[0]
            new = pa.Table.from_pylist(
                [{"key": op.key, "value": row["value"] + op.delta, "applied": [*row["applied"], oid]}],
                schema=table.schema().as_arrow(),
            )
            table.upsert(new, join_cols=["key"])
        except Exception as error:
            outcome, cls, detail = classify(error, arm.definite_errors)
            return Attempted(outcome, 1, cls, detail)
        return Attempted(Outcome.ACKED, 1)

    return add


W2_PY = Arm("w2-py", writes=pyiceberg_writes)


def run_appender(
    table: Any,
    *,
    scenario: Scenario,
    log: OpLogWriter,
    start: float,
    deadline: float,
    definite: frozenset[str] = frozenset(),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Appends a batch of `scenario.appender_batch` events every `appender_every_seconds` until the deadline; each
    batch is one commit and one group. Returns the number of batches sent."""
    keys, batch, seq, sent = list(scenario.seeded_keys()), scenario.appender_batch, 0, 0
    schema = table.schema().as_arrow()
    while (now := clock()) < deadline:
        day = scenario.event_day(0, now - start)
        group = f"{log.writer}:commit-{sent + 1}"
        rows = []
        for i in range(batch):
            seq += 1
            key, delta = keys[(seq * 7 + i) % len(keys)], DELTAS[seq % len(DELTAS)]
            log.invoke(seq, "events", key, delta, group=group)
            rows.append({"writer_id": log.writer, "seq": seq, "key": key, "delta": delta, "event_day": day})
        try:
            table.append(pa.Table.from_pylist(rows, schema=schema))
            outcome, cls, detail = Outcome.ACKED, None, None
        except Exception as error:
            outcome, cls, detail = classify(error, definite)
        latency = round((clock() - now) * 1000, 3)
        for row in rows:
            log.complete(
                op_id(log.writer, row["seq"]), outcome, attempts=1, error=cls, detail=detail, latency_ms=latency
            )
        sent += 1
        pause = min(start + sent * scenario.appender_every_seconds, deadline) - clock()
        if pause > 0:
            sleep(pause)
    return sent
