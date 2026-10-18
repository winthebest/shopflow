"""lakeops.fingerprint without a stack: the SQL of checksum_at, now and at a snapshot."""

from lakeops.fingerprint import checksum_at


class Conn:
    """A DB-API connection that answers the column listing and records the checksum query."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self._last = ""

    def cursor(self) -> "Conn":
        return self

    def execute(self, sql: str) -> None:
        self.received.append(sql)
        self._last = sql

    def fetchall(self) -> list:
        return [["id"], ["v"]] if "information_schema.columns" in self._last else [[0, None]]


def test_checksum_at_reads_the_snapshot_asked_for():
    conn = Conn()
    assert checksum_at(conn, "lab_exp.s.t") == (0, "")
    assert checksum_at(conn, "lab_exp.s.t", 42) == (0, "")
    now, then = conn.received[1], conn.received[3]
    assert now == 'SELECT count(*), to_hex(checksum(ROW("id", "v"))) FROM "lab_exp"."s"."t"'
    assert then == now + " FOR VERSION AS OF 42"
