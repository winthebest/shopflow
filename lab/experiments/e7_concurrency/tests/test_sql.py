"""The statements the writers send, exactly; and the inputs they refuse."""

from datetime import date

import pytest
from e7_concurrency import sql


def test_tables_are_recreated_in_the_lab_catalog_as_format_v2():
    statements = sql.recreate_tables()
    assert statements[:3] == [
        "CREATE SCHEMA IF NOT EXISTS lab_exp.lab_e7",
        "DROP TABLE IF EXISTS lab_exp.lab_e7.events",
        "DROP TABLE IF EXISTS lab_exp.lab_e7.counters",
    ]
    events, counters = statements[3:]
    assert "partitioning = ARRAY['bucket(key, 8)', 'event_day']" in events
    assert "applied array(varchar) NOT NULL" in counters
    assert all("format_version = 2" in s for s in (events, counters))
    assert "applied" not in sql.recreate_tables(ledger=False)[4]


def test_counters_are_seeded_in_one_statement():
    assert sql.seed_counters(range(4)) == (
        "INSERT INTO lab_exp.lab_e7.counters SELECT k, 0, CAST(ARRAY[] AS array(varchar)) "
        "FROM UNNEST(sequence(0, 3)) AS t(k)"
    )
    assert "CAST" not in sql.seed_counters(range(4), ledger=False)
    with pytest.raises(ValueError, match="contiguous"):
        sql.seed_counters(range(0, 8, 2))


def test_baseline_writes():
    assert sql.insert_event("w1-0", 17, 5, 3, date(2026, 1, 2)) == (
        "INSERT INTO lab_exp.lab_e7.events VALUES ('w1-0', 17, 5, 3, DATE '2026-01-02')"
    )
    assert sql.add_to_counter("w2-1:9", 5, 3) == (
        "MERGE INTO lab_exp.lab_e7.counters t USING (VALUES (5, 3)) AS s(key, delta) ON t.key = s.key "
        "WHEN MATCHED THEN UPDATE SET value = t.value + s.delta, applied = t.applied || 'w2-1:9'"
    )
    assert "applied" not in sql.add_to_counter("w2-1:9", 5, 3, ledger=False)


@pytest.mark.parametrize(
    "call",
    [
        lambda: sql.insert_event("w1'; DROP", 1, 1, 1, date(2026, 1, 1)),
        lambda: sql.insert_event("w1-0", "1", 1, 1, date(2026, 1, 1)),
        lambda: sql.insert_event("w1-0", 1, True, 1, date(2026, 1, 1)),
        lambda: sql.add_to_counter("w2-0:1' OR '1", 1, 1),
        lambda: sql.add_to_counter("w2-0:1", 1, 1.5),
    ],
)
def test_anything_but_harness_ids_and_integers_is_refused(call):
    with pytest.raises((ValueError, TypeError)):
        call()


def test_optimize_on_a_table_or_on_past_days():
    assert sql.optimize(sql.COUNTERS) == "ALTER TABLE lab_exp.lab_e7.counters EXECUTE optimize"
    assert sql.optimize(sql.EVENTS, before_day=date(2026, 1, 3)) == (
        "ALTER TABLE lab_exp.lab_e7.events EXECUTE optimize WHERE event_day < DATE '2026-01-03'"
    )
    with pytest.raises(ValueError, match="day partition"):
        sql.optimize(sql.COUNTERS, before_day=date(2026, 1, 3))
    with pytest.raises(ValueError, match="not an experiment table"):
        sql.optimize("lab_exp.other.t")


def test_snapshots_are_read_from_the_metadata_table():
    assert sql.snapshots(sql.EVENTS) == (
        'SELECT snapshot_id, operation, summary FROM lab_exp.lab_e7."events$snapshots" ORDER BY committed_at'
    )
