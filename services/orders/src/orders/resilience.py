"""Retry inside a deadline and a circuit breaker, for orders -> payments (ADR 0102). Own code, no dependency: the
retry must fit inside the 800ms contract deadline, and both need a clock and a random source that tests can drive.

The breaker counts every attempt. Closed: calls go through; once the sliding window holds at least `min_calls`
attempts and `failure_ratio` of them failed, it opens. Open: calls are refused for `open_s`, then it is half-open.
Half-open: exactly one probe goes through (everyone else is refused); its result closes or re-opens the breaker.
One breaker per orders process: each replica decides from what it sees itself.
"""

import math
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum


class CircuitState(IntEnum):
    # The values are what the `orders.payments.circuit.state` gauge reports.
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


@dataclass(eq=False)
class Permit:
    """Leave to make one call. A half-open probe must be recorded or released, or no other probe can start."""

    probe: bool
    done: bool = False


@dataclass(frozen=True)
class BreakerConfig:
    """Opens when payments is essentially down, not when part of it is: with one of two payments pods slow, each
    attempt fails with probability 0.5 however many retries there are, so a 0.5 threshold would open the circuit
    exactly when retries cope. 0.75 over at least 20 attempts stays closed then and opens within seconds when every
    attempt fails."""

    window_s: float = 10.0
    min_calls: int = 20
    failure_ratio: float = 0.75
    open_s: float = 5.0


class CircuitBreaker:
    def __init__(
        self,
        config: BreakerConfig,
        clock: Callable[[], float] = time.monotonic,
        on_transition: Callable[[CircuitState, CircuitState], None] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._on_transition = on_transition
        self._state = CircuitState.CLOSED
        self._results: deque[tuple[float, bool]] = deque()  # (time, failed) of attempts while closed
        self._failures = 0  # failed entries in _results
        self._opened_at = 0.0
        self._probe_running = False

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self.config.open_s:
            self._move(CircuitState.HALF_OPEN)
        return self._state

    def observed_state(self) -> CircuitState:
        """The state without moving the breaker: safe from another thread (the metrics exporter)."""
        state, opened_at = self._state, self._opened_at
        if state is CircuitState.OPEN and self._clock() - opened_at >= self.config.open_s:
            return CircuitState.HALF_OPEN
        return state

    def allow(self) -> Permit | None:
        """A permit for one call, or None while the breaker refuses calls."""
        state = self.state
        if state is CircuitState.CLOSED:
            return Permit(probe=False)
        if state is CircuitState.HALF_OPEN and not self._probe_running:
            self._probe_running = True
            return Permit(probe=True)
        return None

    def record(self, permit: Permit, *, failed: bool) -> None:
        if permit.done:
            return
        permit.done = True
        if permit.probe:
            self._probe_running = False
            self._move(CircuitState.OPEN if failed else CircuitState.CLOSED)
            return
        if self._state is not CircuitState.CLOSED:
            return  # admitted before the breaker opened: the decision is already made
        now = self._clock()
        self._results.append((now, failed))
        self._failures += failed
        while self._results and self._results[0][0] <= now - self.config.window_s:
            self._failures -= self._results.popleft()[1]
        calls = len(self._results)
        if calls >= self.config.min_calls and self._failures >= self.config.failure_ratio * calls:
            self._move(CircuitState.OPEN)

    def release(self, permit: Permit) -> None:
        """Give back a permit that was not used for a call (the request ended before it), freeing the probe slot."""
        if not permit.done:
            permit.done = True
            if permit.probe:
                self._probe_running = False

    def retry_after_s(self) -> int:
        """Whole seconds until the next probe may run (for a `Retry-After` header); at least 1."""
        remaining = self.config.open_s - (self._clock() - self._opened_at)
        return max(1, math.ceil(remaining))

    def _move(self, new: CircuitState) -> None:
        if new is CircuitState.OPEN:
            self._opened_at = self._clock()  # before the state: a reader never sees OPEN with a stale opening time
        if new is CircuitState.CLOSED:
            self._results.clear()
            self._failures = 0
        old, self._state = self._state, new
        if self._on_transition is not None and new is not old:
            self._on_transition(old, new)


@dataclass(frozen=True)
class RetryPolicy:
    """Up to `attempts` calls, each limited to `attempt_timeout_s` and to what is left of the overall deadline.

    The attempt timeout sits well above the dependency's normal latency (payments: ~50ms) but leaves room for one
    retry after a timeout: a uniformly slow dependency (slower than the attempt timeout, faster than the deadline)
    fails where a single call would have succeeded, so the timeout must not be close to normal latency.
    """

    attempts: int = 3
    attempt_timeout_s: float = 0.5
    min_attempt_s: float = 0.1  # no new attempt with less time than this left: it could not finish
    backoff_base_s: float = 0.05
    backoff_cap_s: float = 0.2

    def backoff_s(self, retry: int, rng: random.Random) -> float:
        """Full jitter before retry number `retry` (1, 2, ...): uniform in [0, min(cap, base * 2^(retry-1))]."""
        return rng.uniform(0, min(self.backoff_cap_s, self.backoff_base_s * 2 ** (retry - 1)))
