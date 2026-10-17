"""The op streams: fixed by the seed, disjoint at low contention, assigned by the arm at the shared levels."""

from datetime import date

import pytest
from e7_concurrency.scenario import KEYS_HIGH, KEYS_MEDIUM, Scenario, op_streams


def scenario(**overrides) -> Scenario:
    values = {"name": "t", "workload": "w2", "writers": 4, "contention": "medium", "seed": 7, "ops_per_writer": 50}
    return Scenario(**(values | overrides))


def test_the_seed_fixes_every_stream():
    assert op_streams(scenario()) == op_streams(scenario())
    assert op_streams(scenario()) != op_streams(scenario(seed=8))


def test_seq_counts_from_one_in_each_writer():
    for stream in op_streams(scenario()):
        assert [op.seq for op in stream] == list(range(1, len(stream) + 1))


def test_low_contention_gives_each_writer_its_own_keys_and_days():
    s = scenario(contention="low")
    keys = [{op.key for op in stream} for stream in op_streams(s)]
    assert all(a.isdisjoint(b) for i, a in enumerate(keys) for b in keys[i + 1 :])
    assert set().union(*keys) <= set(s.seeded_keys())
    days = [s.event_day(writer, 25.0) for writer in range(s.writers)]
    assert len(set(days)) == s.writers


@pytest.mark.parametrize(("contention", "keys"), [("medium", KEYS_MEDIUM), ("high", KEYS_HIGH)])
def test_shared_levels_draw_from_one_keyspace(contention: str, keys: int):
    s = scenario(contention=contention)
    assert s.seeded_keys() == range(keys)
    assert {op.key for stream in op_streams(s) for op in stream} <= set(range(keys))


def test_both_assignments_split_the_same_stream():
    """Round robin and an assignment by key hand out the same ops (key, delta), only to different writers."""
    s = scenario(contention="high")
    by_key = op_streams(s, assign=lambda _index, key, n: key % n)
    for writer, stream in enumerate(by_key):
        assert all(op.key % s.writers == writer for op in stream)

    def multiset(streams: list) -> list:
        return sorted((op.key, op.delta) for stream in streams for op in stream)

    assert multiset(by_key) == multiset(op_streams(s))


def test_an_assignment_outside_the_writers_is_refused():
    with pytest.raises(ValueError, match="writer 9 of 4"):
        op_streams(scenario(), assign=lambda _index, _key, _n: 9)


def test_deltas_are_positive():
    assert all(1 <= op.delta <= 9 for stream in op_streams(scenario()) for op in stream)


def test_the_event_day_moves_forward_during_the_write_phase():
    s = scenario(contention="medium", day_seconds=10.0)
    assert [s.event_day(0, t) for t in (0.0, 9.9, 10.0, 31.0)] == [
        date(2026, 1, 1),
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 4),
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"writers": 0}, "at least one writer"),
        ({"contention": "extreme"}, "unknown contention"),
        ({"workload": "w1", "ledger": False}, "only the counters workload"),
    ],
)
def test_invalid_scenarios_are_refused(overrides: dict, message: str):
    with pytest.raises(ValueError, match=message):
        scenario(**overrides)
