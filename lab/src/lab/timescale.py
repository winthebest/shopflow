"""Compressed time: one published factor, applied by the harness to the SQL of every arm alike.

Snapshot and file times are real commit times: Trino and PyIceberg stamp each commit with the wall clock, and nothing
writes a snapshot dated in the past through them. So a lab experiment cannot replay weeks of history in real time; it
runs at a factor published before any result: PUBLISHED, 1 simulated day = 1 real minute (1440 simulated seconds per
real second). Ages and windows on the wall clock are what scales:

- the retention threshold of `EXECUTE expire_snapshots(…)` and `EXECUTE remove_orphan_files(…)`, named
  (`retention_threshold => '<duration>'`) or positional (the first argument): the Trino duration is scaled. A call
  without a threshold is refused: its default (the catalog's, 7 real days) would not be compressed;
- an age from now: `current_timestamp - INTERVAL '<n>' <unit>` (also `current_timestamp(p)`, `localtimestamp`,
  `now()`), e.g. in `FOR TIMESTAMP AS OF` or an `older_than` argument, and `date_add('<unit>', <n>, current_timestamp)`:
  the amount is scaled.

An interval applied to a column is business time, not an age, and stays as written. Ages in weeks, months or years
(no fixed length in Trino's date arithmetic) are refused. The rewrite is lexical: it changes exactly these forms.
The lab's own retention floors are compressed the same way (lab/trino/etc/catalog: 7 simulated days = 7m), and a
test keeps them in step with PUBLISHED.
"""

import re
from dataclasses import dataclass
from datetime import timedelta

_UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600, "d": 86400}
_INTERVAL_UNITS = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400}
_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]+)\s*")
_PROCEDURE = re.compile(r"(\bEXECUTE\s+(?:expire_snapshots|remove_orphan_files)\s*\()([^)]*)(\))", re.IGNORECASE)
_BARE = re.compile(r"\bEXECUTE\s+(?:expire_snapshots|remove_orphan_files)\b(?!\s*\()", re.IGNORECASE)
_NAMED = re.compile(r"(\bretention_threshold\s*=>\s*)'([^']*)'", re.IGNORECASE)
_POSITIONAL = re.compile(r"(\s*)'([^']*)'")
_NOW = r"(?:current_timestamp|localtimestamp)(?:\s*\(\s*\d+\s*\))?|now\s*\(\s*\)"
_AGE = re.compile(rf"(\b(?:{_NOW})\s*-\s*INTERVAL\s*)'(\d+(?:\.\d+)?)'\s*([A-Z]+)\b", re.IGNORECASE)
_DATE_ADD = re.compile(rf"\bdate_add\s*\(\s*'(\w+)'\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*((?:{_NOW}))\s*\)", re.IGNORECASE)
_DATE_ADD_UNITS = {"millisecond": 0.001, "second": 1, "minute": 60, "hour": 3600, "day": 86400}


class TimescaleError(ValueError):
    """A time literal the harness cannot compress."""


def parse_duration(text: str) -> timedelta:
    """A Trino duration ('7d', '2500ms', '1.5h')."""
    match = _DURATION.fullmatch(text)
    if not match or match.group(2) not in _UNITS:
        raise TimescaleError(f"not a Trino duration: {text!r}")
    return timedelta(seconds=float(match.group(1)) * _UNITS[match.group(2)])


def format_duration(value: timedelta) -> str:
    """The shortest exact Trino duration for `value`, at millisecond precision ('7m', '2500ms', '0s')."""
    ms = round(value.total_seconds() * 1000)
    if ms == 0:
        return "0s"
    for unit, ms_per in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)):
        if ms % ms_per == 0:
            return f"{ms // ms_per}{unit}"
    return f"{ms}ms"


@dataclass(frozen=True)
class Timescale:
    factor: float  # simulated seconds per real second

    def real(self, simulated: timedelta) -> timedelta:
        return simulated / self.factor

    def simulated(self, real: timedelta) -> timedelta:
        return real * self.factor

    def describe(self) -> str:
        return f"1 simulated day = {format_duration(self.real(timedelta(days=1)))} real ({self.factor:g}x)"

    def rewrite(self, sql: str) -> str:
        """`sql` with every wall-clock window scaled to real time (the forms in the module docstring)."""

        def duration(text: str) -> str:
            return format_duration(self.real(parse_duration(text)))

        def procedure(match: re.Match) -> str:
            args = match.group(2)
            if _NAMED.search(args):
                args = _NAMED.sub(lambda m: f"{m.group(1)}'{duration(m.group(2))}'", args)
            elif positional := _POSITIONAL.match(args):
                args = f"{positional.group(1)}'{duration(positional.group(2))}'" + args[positional.end() :]
            else:
                raise TimescaleError(
                    f"name the retention threshold (its default is not compressed): {match.group(0)!r}"
                )
            return match.group(1) + args + match.group(3)

        def age(match: re.Match) -> str:
            unit = match.group(3).upper()
            if unit not in _INTERVAL_UNITS:
                raise TimescaleError(f"an age in {unit} has no fixed length; write it in days: {match.group(0)!r}")
            seconds = self.real(timedelta(seconds=float(match.group(2)) * _INTERVAL_UNITS[unit])).total_seconds()
            return f"{match.group(1)}'{seconds:.3f}' SECOND"

        def date_add(match: re.Match) -> str:
            unit = match.group(1).lower()
            if unit not in _DATE_ADD_UNITS:
                raise TimescaleError(f"an age in {unit} has no fixed length; write it in days: {match.group(0)!r}")
            ms = self.real(timedelta(seconds=float(match.group(2)) * _DATE_ADD_UNITS[unit])).total_seconds() * 1000
            return f"date_add('millisecond', {round(ms)}, {match.group(3)})"

        if bare := _BARE.search(sql):
            raise TimescaleError(f"name the retention threshold (its default is not compressed): {bare.group(0)!r}")
        return _DATE_ADD.sub(date_add, _AGE.sub(age, _PROCEDURE.sub(procedure, sql)))


PUBLISHED = Timescale(factor=1440)
REAL_TIME = Timescale(factor=1)
