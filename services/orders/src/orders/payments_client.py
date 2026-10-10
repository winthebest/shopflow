"""Call the payments provider within the 800ms contract deadline and classify the answer. Never raises.

One charge = up to `RetryPolicy.attempts` attempts inside the deadline, with full-jitter backoff, each admitted by
the circuit breaker (ADR 0102). Retried: attempt timeouts, transport errors, 502/503/504; payments is idempotent per
`order_id`, so a retry never charges twice. Not retried: 201, 402 (a decline is a business outcome) and any other
answer. Every attempt counts for the breaker; every answer except 201/402 counts as a failure.

Retries go through `retry_http`, a client without keep-alive: kube-proxy picks a backend per TCP connection, so a
retry over an idle pooled connection could reach the very pod that just timed out. A new connection lets it choose
again (the timed-out connection itself is closed by httpx, its response never having completed).
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import httpx
from opentelemetry import metrics

from orders.resilience import BreakerConfig, CircuitBreaker, CircuitState, Permit, RetryPolicy

PAYMENTS_TIMEOUT_S = 0.8  # contract: orders -> payments 800ms, all attempts included
RETRYABLE_STATUS = frozenset({502, 503, 504})

log = logging.getLogger("orders")


@dataclass(frozen=True)
class ChargeOutcome:
    status: Literal["succeeded", "declined", "error"]
    charge_id: str | None = None
    error: Literal["timeout", "unavailable"] | None = None


@dataclass(frozen=True)
class _Attempt:
    outcome: ChargeOutcome
    retryable: bool

    @property
    def label(self) -> str:  # `outcome` attribute of the attempts counter
        return self.outcome.error or self.outcome.status


class PaymentsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        policy: RetryPolicy | None = None,
        breaker: BreakerConfig | None = None,
        *,
        retry_http: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        meter: metrics.Meter | None = None,
    ) -> None:
        self.http = http
        self.retry_http = retry_http or http
        self.policy = policy or RetryPolicy()
        self.breaker = CircuitBreaker(breaker or BreakerConfig(), clock=clock, on_transition=self._transitioned)
        self._rng = rng or random.Random()  # noqa: S311 - backoff jitter, not security
        self._sleep = sleep
        self._clock = clock
        # Low cardinality on purpose: outcome / attempt number / target state, never an order id.
        meter = meter or metrics.get_meter("orders")
        self._attempts = meter.create_counter(
            "orders.payments.attempts", unit="{attempt}", description="Calls to payments, by outcome and attempt"
        )
        self._rejected = meter.create_counter(
            "orders.payments.circuit.rejected",
            unit="{checkout}",
            description="Checkouts refused with 503 because the circuit is open",
        )
        self._transitions = meter.create_counter(
            "orders.payments.circuit.transitions", unit="{transition}", description="Circuit state changes"
        )
        meter.create_observable_gauge(
            "orders.payments.circuit.state",
            callbacks=[self._observe_state],
            description="Circuit of this process: 0 closed, 1 half-open, 2 open",
        )

    def admit(self) -> Permit | None:
        """Leave for the first attempt, taken before the order is created; None (the checkout gets a 503) while the
        circuit is open."""
        permit = self.breaker.allow()
        if permit is None:
            self._rejected.add(1)
        return permit

    def release(self, permit: Permit) -> None:
        """The checkout ended before charging (invalid input, database error): hand the permit back."""
        self.breaker.release(permit)

    async def charge(self, order_id: int, amount: Decimal, permit: Permit) -> ChargeOutcome:
        deadline = self._clock() + PAYMENTS_TIMEOUT_S
        attempt = 1
        while True:
            timeout_s = min(self.policy.attempt_timeout_s, deadline - self._clock())
            http = self.http if attempt == 1 else self.retry_http
            try:
                result = await self._call(http, order_id, amount, timeout_s)
            except BaseException:  # cancelled: never leave a half-open probe slot taken
                self.breaker.release(permit)
                raise
            self.breaker.record(permit, failed=result.outcome.error is not None)
            self._attempts.add(1, {"outcome": result.label, "attempt": attempt})
            if not result.retryable or attempt >= self.policy.attempts:
                return result.outcome
            delay = self.policy.backoff_s(attempt, self._rng)
            if deadline - self._clock() - delay < self.policy.min_attempt_s:
                return result.outcome
            await self._sleep(delay)
            if deadline - self._clock() < self.policy.min_attempt_s:  # the loop was slower than the backoff
                return result.outcome
            next_permit = self.breaker.allow()
            if next_permit is None:  # opened meanwhile: stop retrying, keep the last answer
                return result.outcome
            permit, attempt = next_permit, attempt + 1

    async def _call(self, http: httpx.AsyncClient, order_id: int, amount: Decimal, timeout_s: float) -> _Attempt:
        try:
            async with asyncio.timeout(timeout_s):
                response = await http.post("/charges", json={"order_id": order_id, "amount": str(amount)})
        except (TimeoutError, httpx.TimeoutException):
            return _Attempt(ChargeOutcome("error", error="timeout"), retryable=True)
        except httpx.HTTPError:
            return _Attempt(ChargeOutcome("error", error="unavailable"), retryable=True)

        if response.status_code == 201:
            try:
                return _Attempt(ChargeOutcome("succeeded", charge_id=str(response.json()["charge_id"])), False)
            except (ValueError, KeyError, TypeError):  # answered, but not in the agreed shape: treat as no answer
                return _Attempt(ChargeOutcome("error", error="unavailable"), retryable=False)
        if response.status_code == 402:
            return _Attempt(ChargeOutcome("declined"), retryable=False)
        return _Attempt(ChargeOutcome("error", error="unavailable"), response.status_code in RETRYABLE_STATUS)

    def _transitioned(self, old: CircuitState, new: CircuitState) -> None:
        self._transitions.add(1, {"to": new.name.lower()})
        level = logging.WARNING if new is CircuitState.OPEN else logging.INFO
        states = {"circuit_from": old.name.lower(), "circuit_to": new.name.lower()}
        log.log(level, "payments circuit %s", states["circuit_to"], extra=states)

    def _observe_state(self, _options: metrics.CallbackOptions) -> Iterable[metrics.Observation]:
        yield metrics.Observation(int(self.breaker.observed_state()))  # exporter thread: must not move the breaker
