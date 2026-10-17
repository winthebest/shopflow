"""Three-state op log: every op ends acked, definite-fail or unknown.

Each writer process appends JSON lines to its own file:

    {"type": "invoke", "op": "w2-0:17", "table": "counters", "key": 5, "delta": 3, "group": null, "at": 1.7e9}
    {"type": "complete", "op": "w2-0:17", "outcome": "acked", "attempts": 2, "error": null, "latency_ms": 41.2,
     "query_ids": ["20261018_..."], "snapshot_id": 123}

The invoke line is written before the request leaves the client, and the file is line-buffered, so the line is in the
kernel's page cache even if the writer is killed (SIGKILL) a moment later. An invoke without a complete line reads back
as unknown: the request may have committed on the server. A torn last line (the process died inside `write`) is
ignored; for an invoke that means the request was never sent, for a complete that the op stays unknown.
"""

import contextlib
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, TextIO

Table = Literal["events", "counters"]
TABLES: tuple[Table, ...] = ("events", "counters")


class Outcome(StrEnum):
    ACKED = "acked"  # the server reported success to the client
    FAILED = "definite-fail"  # the server reported a failure that guarantees nothing committed
    UNKNOWN = "unknown"  # anything else: timeout, lost connection, killed client, an error that may follow a commit


@dataclass(frozen=True)
class Op:
    """One logical op (all its attempts) as the client saw it."""

    writer: str
    seq: int
    table: Table
    key: int
    delta: int
    outcome: Outcome
    group: str | None = None  # ops sent in one statement or commit share a group: all of them commit, or none
    attempts: int = 0
    error: str | None = None  # error class of the last attempt, e.g. a Trino error name
    latency_ms: float | None = None
    query_ids: tuple[str, ...] = ()
    snapshot_id: int | None = None

    @property
    def id(self) -> str:
        return op_id(self.writer, self.seq)

    @property
    def commit(self) -> str:
        """The unit that commits: the group if the op has one, else the op itself."""
        return self.group or self.id


def op_id(writer: str, seq: int) -> str:
    return f"{writer}:{seq}"


class OpLogError(ValueError):
    """The log breaks its own rules (an op id reused, a complete without invoke): a bug in the runner, not data."""


class OpLogWriter:
    """Appends one writer's records; `invoke` before sending the request, `complete` once the outcome is known."""

    def __init__(self, path: Path, writer: str) -> None:
        if ":" in writer:
            raise OpLogError(f"writer id {writer!r} must not contain ':'")
        self.writer = writer
        # Line-buffered: each record reaches the kernel with one write when its newline is written.
        self._file: TextIO = path.open("a", encoding="utf-8", buffering=1)

    def invoke(self, seq: int, table: Table, key: int, delta: int, group: str | None = None) -> str:
        if table not in TABLES:
            raise OpLogError(f"unknown table {table!r}")
        oid = op_id(self.writer, seq)
        self._write(
            {"type": "invoke", "op": oid, "table": table, "key": key, "delta": delta, "group": group, "at": time.time()}
        )
        return oid

    def complete(
        self,
        oid: str,
        outcome: Outcome,
        *,
        attempts: int,
        error: str | None = None,
        latency_ms: float | None = None,
        query_ids: Sequence[str] = (),
        snapshot_id: int | None = None,
    ) -> None:
        self._write(
            {
                "type": "complete",
                "op": oid,
                "outcome": Outcome(outcome).value,
                "attempts": attempts,
                "error": error,
                "latency_ms": latency_ms,
                "query_ids": list(query_ids),
                "snapshot_id": snapshot_id,
            }
        )

    def _write(self, record: dict) -> None:
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "OpLogWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _records(path: Path) -> Iterable[dict]:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    # A file that does not end in a newline has a torn last record.
    torn = lines.pop() if lines else ""
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as error:
            raise OpLogError(f"{path}:{number}: not a JSON record") from error
    if torn.strip():
        with contextlib.suppress(json.JSONDecodeError):  # killed inside write: see the module docstring
            yield json.loads(torn)


def read_oplog(paths: Iterable[Path]) -> list[Op]:
    """Merges the writers' files into one op per invoke, in file order."""
    invokes: dict[str, dict] = {}
    completes: dict[str, dict] = {}
    for path in paths:
        for record in _records(path):
            oid = record.get("op")
            kind = record.get("type")
            if kind == "invoke":
                if oid in invokes:
                    raise OpLogError(f"{path}: op {oid} invoked twice")
                if record.get("table") not in TABLES:
                    raise OpLogError(f"{path}: op {oid} on unknown table {record.get('table')!r}")
                invokes[oid] = record
            elif kind == "complete":
                if oid not in invokes:
                    raise OpLogError(f"{path}: op {oid} completed without an invoke")
                if oid in completes:
                    raise OpLogError(f"{path}: op {oid} completed twice")
                completes[oid] = record
            else:
                raise OpLogError(f"{path}: unknown record type {kind!r}")

    ops = []
    for oid, inv in invokes.items():
        writer, _, seq = oid.rpartition(":")
        done = completes.get(oid)
        if done is None:
            outcome, extra = Outcome.UNKNOWN, {"error": "no-completion"}
        else:
            outcome = Outcome(done["outcome"])
            extra = {
                "attempts": done["attempts"],
                "error": done["error"],
                "latency_ms": done["latency_ms"],
                "query_ids": tuple(done["query_ids"]),
                "snapshot_id": done["snapshot_id"],
            }
        ops.append(
            Op(
                writer=writer,
                seq=int(seq),
                table=inv["table"],
                key=inv["key"],
                delta=inv["delta"],
                group=inv["group"],
                outcome=outcome,
                **extra,
            )
        )
    return ops
