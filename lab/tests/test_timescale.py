"""lab.timescale: the published factor, Trino durations, the SQL rewrite, and the lab floors kept in step with it."""

from datetime import timedelta

import pytest

from lab.guard import LakeGuardError, read_properties
from lab.session import ArmSession
from lab.timescale import PUBLISHED, REAL_TIME, Timescale, TimescaleError, format_duration, parse_duration
from lab.trino_client import REPO

# Catalogs whose floors are 7 simulated days; lab_exp (the harness's) has none.
FLOORED = ["lab_ops", "lab_ro", "lab_sbx"]
FLOOR_PROPERTIES = ["iceberg.expire-snapshots.min-retention", "iceberg.remove-orphan-files.min-retention"]


def test_the_published_scale_is_one_simulated_day_per_real_minute():
    assert PUBLISHED.real(timedelta(days=1)) == timedelta(minutes=1)
    assert PUBLISHED.simulated(timedelta(minutes=7)) == timedelta(days=7)
    assert PUBLISHED.describe() == "1 simulated day = 1m real (1440x)"


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("7d", 604800), ("0s", 0), ("2500ms", 2.5), ("1.5h", 5400), (" 30 m ", 1800), ("10us", 1e-5)],
)
def test_trino_durations_parse(text: str, seconds: float):
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["7", "7 days", "d", "-1d", "1w", ""])
def test_other_text_is_not_a_duration(text: str):
    with pytest.raises(TimescaleError):
        parse_duration(text)


@pytest.mark.parametrize(
    ("value", "text"),
    [(timedelta(days=7), "7d"), (timedelta(minutes=7), "7m"), (timedelta(seconds=2.5), "2500ms"), (timedelta(0), "0s")],
)
def test_durations_format_exactly_and_short(value: timedelta, text: str):
    assert format_duration(value) == text
    assert parse_duration(text) == value


def test_retention_thresholds_are_scaled():
    sql = "ALTER TABLE lab_exp.s.t EXECUTE expire_snapshots(retention_threshold => '7d')"
    assert PUBLISHED.rewrite(sql) == "ALTER TABLE lab_exp.s.t EXECUTE expire_snapshots(retention_threshold => '7m')"
    sql = "ALTER TABLE t EXECUTE remove_orphan_files(RETENTION_THRESHOLD=>'1h')"
    assert PUBLISHED.rewrite(sql) == "ALTER TABLE t EXECUTE remove_orphan_files(RETENTION_THRESHOLD=>'2500ms')"


def test_positional_thresholds_are_scaled_and_missing_ones_refused():
    sql = "ALTER TABLE t EXECUTE expire_snapshots('7d')"
    assert PUBLISHED.rewrite(sql) == "ALTER TABLE t EXECUTE expire_snapshots('7m')"
    for sql in ("ALTER TABLE t EXECUTE expire_snapshots", "ALTER TABLE t EXECUTE remove_orphan_files()"):
        with pytest.raises(TimescaleError, match="name the retention threshold"):
            PUBLISHED.rewrite(sql)
    assert PUBLISHED.rewrite("ALTER TABLE t EXECUTE optimize(file_size_threshold => '10MB')") == (
        "ALTER TABLE t EXECUTE optimize(file_size_threshold => '10MB')"
    )


def test_date_add_ages_and_precise_timestamps_are_scaled():
    assert PUBLISHED.rewrite("date_add('day', -3, current_timestamp)") == (
        "date_add('millisecond', -180000, current_timestamp)"
    )
    assert (
        PUBLISHED.rewrite("current_timestamp(6) - INTERVAL '3' DAY")
        == "current_timestamp(6) - INTERVAL '180.000' SECOND"
    )
    with pytest.raises(TimescaleError, match="no fixed length"):
        PUBLISHED.rewrite("date_add('week', -1, now())")
    assert PUBLISHED.rewrite("date_add('day', 1, order_date)") == "date_add('day', 1, order_date)"  # business time


def test_ages_from_now_are_scaled():
    sql = "SELECT * FROM t FOR TIMESTAMP AS OF current_timestamp - INTERVAL '3' DAY"
    assert PUBLISHED.rewrite(sql) == "SELECT * FROM t FOR TIMESTAMP AS OF current_timestamp - INTERVAL '180.000' SECOND"
    assert PUBLISHED.rewrite("now() - interval '12' hour") == "now() - interval '30.000' SECOND"
    assert PUBLISHED.rewrite("localtimestamp - INTERVAL '1.5' MINUTE") == "localtimestamp - INTERVAL '0.062' SECOND"


def test_business_time_and_other_literals_stay():
    sql = (
        "SELECT * FROM orders WHERE created_at > DATE '2026-10-01' - INTERVAL '7' DAY "
        "AND note = 'retention_threshold' AND v = '7d'"
    )
    assert PUBLISHED.rewrite(sql) == sql


def test_year_and_month_ages_are_refused():
    for sql in ("current_timestamp - INTERVAL '1' MONTH", "now() - INTERVAL '2' YEAR"):
        with pytest.raises(TimescaleError, match="no fixed length"):
            PUBLISHED.rewrite(sql)


def test_real_time_changes_nothing_but_the_spelling():
    assert REAL_TIME.rewrite("ALTER TABLE t EXECUTE expire_snapshots(retention_threshold => '168h')") == (
        "ALTER TABLE t EXECUTE expire_snapshots(retention_threshold => '7d')"
    )
    assert Timescale(factor=2).real(timedelta(hours=1)) == timedelta(minutes=30)


@pytest.mark.parametrize("catalog", FLOORED)
def test_the_lab_floors_are_seven_simulated_days(catalog: str):
    properties = read_properties(REPO / f"lab/trino/etc/catalog/{catalog}.properties")
    assert [properties[p] for p in FLOOR_PROPERTIES] == [format_duration(PUBLISHED.real(timedelta(days=7)))] * 2


class Recorder:
    """A DB-API connection that records the SQL it receives (what Trino would run)."""

    def __init__(self) -> None:
        self.received: list[str] = []

    def cursor(self) -> "Recorder":
        return self

    def execute(self, sql: str) -> None:
        self.received.append(sql)

    def fetchall(self) -> list:
        return []


def test_an_arm_session_runs_the_rewritten_sql_and_keeps_both():
    conn = Recorder()
    session = ArmSession(conn)
    written = "ALTER TABLE lab_exp.s.t EXECUTE expire_snapshots(retention_threshold => '7d')"
    session.execute(written)
    run = "ALTER TABLE lab_exp.s.t EXECUTE expire_snapshots(retention_threshold => '7m')"
    assert conn.received == [run]
    assert session.statements == [(written, run)]


def test_the_baseline_template_has_the_same_floors():
    properties = read_properties(REPO / "lab/trino/base/catalog.properties")
    assert [properties[p] for p in FLOOR_PROPERTIES] == [format_duration(PUBLISHED.real(timedelta(days=7)))] * 2


def test_no_arm_session_runs_as_the_capture_identity():
    with pytest.raises(LakeGuardError, match="no arm runs as it"):
        ArmSession.open("observer")
