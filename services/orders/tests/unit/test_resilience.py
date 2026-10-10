"""Circuit breaker transitions and retry backoff, on a fake clock."""

import random

import pytest

from orders.resilience import BreakerConfig, CircuitBreaker, CircuitState, RetryPolicy


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def breaker(clock: Clock, transitions: list | None = None, **config) -> CircuitBreaker:
    settings = {"window_s": 10.0, "min_calls": 4, "failure_ratio": 0.5, "open_s": 5.0, **config}
    on_transition = (lambda old, new: transitions.append((old, new))) if transitions is not None else None
    return CircuitBreaker(BreakerConfig(**settings), clock=clock, on_transition=on_transition)


def call(cb: CircuitBreaker, *, failed: bool) -> None:
    permit = cb.allow()
    assert permit is not None
    cb.record(permit, failed=failed)


def test_stays_closed_below_min_calls_even_if_all_failed():
    cb = breaker(Clock())
    for _ in range(3):
        call(cb, failed=True)
    assert cb.state is CircuitState.CLOSED


def test_opens_at_the_failure_ratio_and_refuses_calls():
    transitions: list = []
    cb = breaker(Clock(), transitions)
    for failed in (False, True, False, True):  # 2 of 4 = 50%
        call(cb, failed=failed)
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is None
    assert transitions == [(CircuitState.CLOSED, CircuitState.OPEN)]


def test_stays_closed_below_the_failure_ratio():
    cb = breaker(Clock())
    for failed in (False, False, False, True, False, True):  # 2 of 6
        call(cb, failed=failed)
    assert cb.state is CircuitState.CLOSED


def test_failures_older_than_the_window_are_forgotten():
    clock = Clock()
    cb = breaker(clock)
    for _ in range(3):
        call(cb, failed=True)
    clock.now += 10.0  # those three leave the window
    for failed in (False, False, False, True):
        call(cb, failed=failed)
    assert cb.state is CircuitState.CLOSED


def opened(clock: Clock, transitions: list | None = None) -> CircuitBreaker:
    cb = breaker(clock, transitions)
    for _ in range(4):
        call(cb, failed=True)
    assert cb.state is CircuitState.OPEN
    return cb


def test_half_open_after_open_s_lets_exactly_one_probe_through():
    clock = Clock()
    cb = opened(clock)
    clock.now += 4.9
    assert cb.allow() is None
    clock.now += 0.1
    probe = cb.allow()
    assert probe is not None and probe.probe
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.allow() is None  # everyone else waits for the probe


def test_observed_state_reports_half_open_without_moving_the_breaker():
    clock, transitions = Clock(), []
    cb = opened(clock, transitions)
    clock.now += 5
    assert cb.observed_state() is CircuitState.HALF_OPEN  # what the gauge reports, from the exporter thread
    assert transitions == [(CircuitState.CLOSED, CircuitState.OPEN)]  # no transition logged or counted


def test_successful_probe_closes_with_a_fresh_window():
    clock, transitions = Clock(), []
    cb = opened(clock, transitions)
    clock.now += 5
    cb.record(cb.allow(), failed=False)
    assert cb.state is CircuitState.CLOSED
    assert [new for _, new in transitions] == [CircuitState.OPEN, CircuitState.HALF_OPEN, CircuitState.CLOSED]
    for _ in range(3):  # the failures from before the opening no longer count
        call(cb, failed=True)
    assert cb.state is CircuitState.CLOSED


def test_failed_probe_reopens_for_another_open_s():
    clock = Clock()
    cb = opened(clock)
    clock.now += 5
    cb.record(cb.allow(), failed=True)
    assert cb.state is CircuitState.OPEN
    assert cb.retry_after_s() == 5
    clock.now += 2.5
    assert cb.retry_after_s() == 3  # rounded up, never 0


def test_late_result_of_a_call_admitted_while_closed_does_not_decide():
    clock = Clock()
    cb = breaker(clock)
    late = cb.allow()
    for _ in range(4):
        call(cb, failed=True)
    clock.now += 5
    probe = cb.allow()
    cb.record(late, failed=False)  # finishes during the probe: must not close the breaker
    assert cb.state is CircuitState.HALF_OPEN
    cb.record(probe, failed=True)
    assert cb.state is CircuitState.OPEN


def test_released_probe_frees_the_slot_and_a_permit_counts_once():
    clock = Clock()
    cb = opened(clock)
    clock.now += 5
    probe = cb.allow()
    cb.release(probe)  # the request ended before calling payments
    second = cb.allow()
    assert second is not None and second.probe
    cb.record(second, failed=False)
    cb.record(second, failed=True)  # ignored: already recorded
    assert cb.state is CircuitState.CLOSED


@pytest.mark.parametrize(("retry", "bound"), [(1, 0.05), (2, 0.1), (3, 0.2), (4, 0.2)])
def test_backoff_is_full_jitter_up_to_the_cap(retry, bound):
    policy, rng = RetryPolicy(), random.Random(7)
    delays = [policy.backoff_s(retry, rng) for _ in range(2000)]
    assert 0 <= min(delays) < bound * 0.05
    assert bound * 0.95 < max(delays) <= bound
