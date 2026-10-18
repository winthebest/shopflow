"""Oracle: grades the final tables against the op log (docs/research/e7-concurrent-writers-design.md).

Rules:
- an acked op is present exactly once;
- a definite-fail op is absent;
- an unknown op is present at most once;
- each seeded key has exactly one counter row, and its value is the sum of the deltas of the ops in its ledger
  (`applied`): the counters equal the sum of the events present;
- ops sent as one group are all present or all absent.

Counts:
- LostUpdate: acked ops that are absent;
- Duplicates: occurrences of any op beyond its first;
- WrongFinalRows: final rows that must not exist, or hold a wrong value; a seeded key with no row counts as one.

A run is valid (its Correct Commit Throughput counts) iff LostUpdate = 0 and WrongFinalRows = 0. Duplicates are
reported on their own, and the method must also have none.

The oracle reads only the op log and the final rows, never a writer's own verdict or retry logic.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields

from .oplog import Op, Outcome, op_id


@dataclass(frozen=True)
class EventRow:
    writer: str
    seq: int
    key: int
    delta: int


@dataclass(frozen=True)
class CounterRow:
    key: int
    value: int
    applied: tuple[str, ...]  # ids of the ops this row's value includes, in the order they were applied


@dataclass(frozen=True)
class Finding:
    kind: str  # lost, duplicate, failed-present, unlogged, altered, ledger-mismatch, extra-row, missing-row, ...
    subject: str  # an op id, or a row reference: events[i], counters[i], counters[key=k]
    detail: str


@dataclass(frozen=True)
class Verdict:
    lost_update: int
    duplicates: int
    wrong_final_rows: int
    acked: int
    failed: int
    unknown: int
    unknown_present: int  # unknown ops that did commit (once)
    unknown_absent: int
    failed_present: int  # part of WrongFinalRows, broken out: the client called a failure definite and it was not
    torn_groups: int
    findings: tuple[Finding, ...]

    @property
    def valid(self) -> bool:
        return self.lost_update == 0 and self.wrong_final_rows == 0

    def counts(self) -> dict[str, int]:
        """The numbers of a results row (everything but the findings)."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "findings"}


def grade(
    ops: Sequence[Op], events: Iterable[EventRow], counters: Iterable[CounterRow], seeded_keys: Iterable[int]
) -> Verdict:
    log: dict[str, Op] = {}
    for op in ops:
        if op.id in log:
            raise ValueError(f"op {op.id} appears twice in the log")
        log[op.id] = op

    seen: dict[str, list[str]] = defaultdict(list)  # op id -> references of the rows that hold it
    wrong: dict[str, None] = {}  # references of wrong rows (an ordered set: a row counts once whatever its faults)
    findings: list[Finding] = []

    def flag(ref: str, kind: str, detail: str) -> None:
        wrong.setdefault(ref)
        findings.append(Finding(kind, ref, detail))

    for i, row in enumerate(events):
        ref, oid = f"events[{i}]", op_id(row.writer, row.seq)
        op = log.get(oid)
        if op is None or op.table != "events":
            flag(ref, "unlogged", f"{oid} is not an events op in the log")
            continue
        seen[oid].append(ref)
        if (row.key, row.delta) != (op.key, op.delta):
            flag(ref, "altered", f"{oid}: row has key {row.key}, delta {row.delta}; the log has {op.key}, {op.delta}")
        if op.outcome is Outcome.FAILED:
            flag(ref, "failed-present", f"{oid} failed definitely, but its row exists")

    rows_by_key: dict[int, list[tuple[str, CounterRow]]] = defaultdict(list)
    for i, row in enumerate(counters):
        rows_by_key[row.key].append((f"counters[{i}]", row))
    seeded = set(seeded_keys)
    for key, rows in rows_by_key.items():
        if key not in seeded:
            for ref, _ in rows:
                flag(ref, "unseeded-key", f"key {key} was never seeded")
        for ref, _ in rows[1:]:
            flag(ref, "extra-row", f"key {key} has {len(rows)} rows")
        for ref, row in rows:
            total = 0
            for oid in row.applied:
                op = log.get(oid)
                if op is None or op.table != "counters" or op.key != key:
                    flag(ref, "unlogged", f"ledger entry {oid} is not a counters op on key {key}")
                    continue
                seen[oid].append(ref)
                total += op.delta
                if op.outcome is Outcome.FAILED:
                    flag(ref, "failed-present", f"{oid} failed definitely, but key {key} applied it")
            if row.value != total:
                flag(ref, "ledger-mismatch", f"key {key}: value {row.value}, its ledger sums to {total}")
    for key in sorted(seeded - rows_by_key.keys()):
        flag(f"counters[key={key}]", "missing-row", f"seeded key {key} has no row")

    lost = duplicates = failed_present = unknown_present = unknown_absent = 0
    for op in ops:
        refs = seen.get(op.id, [])
        if len(refs) > 1:
            duplicates += len(refs) - 1
            findings.append(Finding("duplicate", op.id, f"present {len(refs)} times: {', '.join(refs)}"))
        if op.outcome is Outcome.ACKED and not refs:
            lost += 1
            findings.append(Finding("lost", op.id, f"acked after {op.attempts} attempt(s), absent from {op.table}"))
        elif op.outcome is Outcome.FAILED and refs:
            failed_present += 1
        elif op.outcome is Outcome.UNKNOWN:
            if refs:
                unknown_present += 1
            else:
                unknown_absent += 1

    groups: dict[str, list[Op]] = defaultdict(list)
    for op in ops:
        if op.group:
            groups[op.group].append(op)
    torn = 0
    for group, members in groups.items():
        present = [op for op in members if seen.get(op.id)]
        if present and len(present) < len(members):
            torn += 1
            for op in present:
                for ref in seen[op.id]:
                    flag(ref, "torn-group", f"group {group}: {len(present)} of {len(members)} ops present")

    by_outcome = {outcome: sum(op.outcome is outcome for op in ops) for outcome in Outcome}
    return Verdict(
        lost_update=lost,
        duplicates=duplicates,
        wrong_final_rows=len(wrong),
        acked=by_outcome[Outcome.ACKED],
        failed=by_outcome[Outcome.FAILED],
        unknown=by_outcome[Outcome.UNKNOWN],
        unknown_present=unknown_present,
        unknown_absent=unknown_absent,
        failed_present=failed_present,
        torn_groups=torn,
        findings=tuple(findings),
    )


def acked_commits(ops: Iterable[Op]) -> int:
    """Commits the clients saw succeed: a group counts once. Unknown ops never count, even those that committed."""
    return len({op.commit for op in ops if op.outcome is Outcome.ACKED})


def correct_commit_throughput(verdict: Verdict, ops: Iterable[Op], seconds: float) -> float | None:
    """Acked commits per second of the write phase; None when the run is invalid (it then has no throughput)."""
    if seconds <= 0:
        raise ValueError("the write phase must last a positive time")
    return acked_commits(ops) / seconds if verdict.valid else None


@dataclass(frozen=True)
class BoundsVerdict:
    """The weaker grade of a run without the ledger, used only to measure the ledger's cost.

    Deltas are positive, so per key Σ acked ≤ value ≤ Σ acked + Σ unknown. A value below the range means an acked add
    is missing; above it, an add was counted twice or a failed one applied. A loss and a duplicate on the same key
    can cancel out, which only the ledger would show.
    """

    below: int
    above: int
    wrong_rows: int  # a second row for a key, an unseeded key, a seeded key with no row

    @property
    def valid(self) -> bool:
        return self.below == 0 and self.above == 0 and self.wrong_rows == 0


def grade_bounds(ops: Sequence[Op], values: Iterable[tuple[int, int]], seeded_keys: Iterable[int]) -> BoundsVerdict:
    acked: dict[int, int] = defaultdict(int)
    unknown: dict[int, int] = defaultdict(int)
    for op in ops:
        if op.table != "counters":
            continue
        if op.delta <= 0:
            raise ValueError(f"op {op.id}: bounds need positive deltas")
        if op.outcome is Outcome.ACKED:
            acked[op.key] += op.delta
        elif op.outcome is Outcome.UNKNOWN:
            unknown[op.key] += op.delta
    rows: dict[int, list[int]] = defaultdict(list)
    for key, value in values:
        rows[key].append(value)
    seeded = set(seeded_keys)
    wrong = sum(len(v) - 1 for v in rows.values()) + len(rows.keys() - seeded) + len(seeded - rows.keys())
    below = above = 0
    for key in seeded & rows.keys():
        value = rows[key][0]
        below += value < acked[key]
        above += value > acked[key] + unknown[key]
    return BoundsVerdict(below=below, above=above, wrong_rows=wrong)
