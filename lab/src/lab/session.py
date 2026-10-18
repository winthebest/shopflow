"""The one way an experiment arm runs SQL: the guard admits the connection, the time scale rewrites every statement.

Every arm (method, baseline, agent) gets an ArmSession, so the same compression applies to all of them and none can
skip it. No arm session is ever built as the capture identity (observer), whose reads must stay out of every arm's
view. Sessions never resubmit a statement (max_attempts=1): a resubmitted write would run twice.

Every statement is logged as a StatementRecord, what the run's statements.jsonl holds: identity, the session's
catalog, the SQL as written and as run, Trino's query id, the final state (FINISHED, FAILED, or ERROR when the
client failed before Trino answered), the error, the rows written (Trino's update count for INSERT, UPDATE,
DELETE, MERGE and CREATE TABLE AS; None otherwise), the start time and the elapsed seconds. The log comes from the
harness: each identity sees only its own queries in system.runtime.queries.
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import trino

from lab.guard import CAPTURE_IDENTITY, LakeGuardError, guarded_connect
from lab.timescale import PUBLISHED, Timescale
from lab.trino_client import Settings


@dataclass(frozen=True)
class StatementRecord:
    identity: str
    catalog: str | None
    sql_written: str
    sql_run: str
    query_id: str | None
    state: str
    error_name: str | None
    error_message: str | None
    rows: int | None
    started_at: str
    elapsed_s: float

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class ArmSession:
    conn: trino.dbapi.Connection
    timescale: Timescale = PUBLISHED
    statements: list[StatementRecord] = field(default_factory=list)
    on_statement: Callable[[StatementRecord], None] | None = None  # e.g. the run's log writer

    @classmethod
    def open(
        cls,
        user: str,
        *,
        timescale: Timescale = PUBLISHED,
        require_lab: bool = True,
        settings: Settings | None = None,
        on_statement: Callable[[StatementRecord], None] | None = None,
    ) -> "ArmSession":
        if user == CAPTURE_IDENTITY:
            raise LakeGuardError(f"{CAPTURE_IDENTITY} captures state for the scorer; no arm runs as it")
        conn = guarded_connect(user, require_lab=require_lab, settings=settings, max_attempts=1)
        return cls(conn, timescale, on_statement=on_statement)

    def execute(self, sql: str) -> list:
        run = self.timescale.rewrite(sql)
        started, start = datetime.now(UTC), time.perf_counter()
        cur = self.conn.cursor()
        state, error_name, error_message, result = "FINISHED", None, None, []
        try:
            cur.execute(run)
            result = cur.fetchall()
        except trino.exceptions.TrinoQueryError as error:
            state, error_name, error_message = "FAILED", error.error_name, error.message
            raise
        except Exception as error:
            state, error_name, error_message = "ERROR", type(error).__name__, str(error)
            raise
        finally:
            rowcount = getattr(cur, "rowcount", -1)
            record = StatementRecord(
                identity=self.conn.user,
                catalog=getattr(self.conn, "catalog", None),
                sql_written=sql,
                sql_run=run,
                query_id=getattr(cur, "query_id", None),
                state=state,
                error_name=error_name,
                error_message=error_message,
                rows=rowcount if isinstance(rowcount, int) and rowcount >= 0 else None,
                started_at=started.isoformat(timespec="milliseconds"),
                elapsed_s=round(time.perf_counter() - start, 6),
            )
            self.statements.append(record)
            if self.on_statement:
                self.on_statement(record)
        return result
