"""The one way an experiment arm runs SQL: the guard admits the connection, the time scale rewrites every statement.

Every arm (method, baseline, agent) gets an ArmSession, so the same compression applies to all of them and none can
skip it. The statements are kept as (written, run) pairs for the run's record.
"""

from dataclasses import dataclass, field

import trino

from lab.guard import guarded_connect
from lab.timescale import PUBLISHED, Timescale
from lab.trino_client import Settings, query


@dataclass
class ArmSession:
    conn: trino.dbapi.Connection
    timescale: Timescale = PUBLISHED
    statements: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def open(
        cls,
        user: str,
        *,
        timescale: Timescale = PUBLISHED,
        require_lab: bool = True,
        settings: Settings | None = None,
    ) -> "ArmSession":
        return cls(guarded_connect(user, require_lab=require_lab, settings=settings), timescale)

    def execute(self, sql: str) -> list:
        run = self.timescale.rewrite(sql)
        self.statements.append((sql, run))
        return query(self.conn, run)
