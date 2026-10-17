"""A stand-in for a Trino DB-API connection in the unit tests: it records each statement and answers from a function.

The connectors at the bottom are module-level so that writer processes started by `runner.launch` (spawn) can import
them; each process gets its own fake, so they only exercise the processes and their op logs, not a table.
"""

import time
from collections.abc import Callable

Respond = Callable[[str], list | BaseException]


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self.query_id: str | None = None
        self._rows: list = []

    def execute(self, statement: str) -> None:
        self.conn.statements.append(statement)
        self.query_id = f"q{len(self.conn.statements)}"  # like Trino: the query has an id before it can fail
        result = self.conn.respond(statement)
        if isinstance(result, BaseException):
            raise result
        self._rows = result

    def fetchall(self) -> list:
        return self._rows


class FakeConnection:
    def __init__(self, respond: Respond = lambda _statement: [[1]]) -> None:
        self.statements: list[str] = []
        self.respond = respond

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def _sleeping(seconds: float) -> Respond:
    def respond(_statement: str) -> list:
        time.sleep(seconds)
        return [[1]]

    return respond


def quick_connector(_arm: object) -> FakeConnection:
    return FakeConnection(_sleeping(0.01))


def slow_connector(_arm: object) -> FakeConnection:
    return FakeConnection(_sleeping(0.2))


def hanging_connector(_arm: object) -> FakeConnection:
    return FakeConnection(_sleeping(3600))
