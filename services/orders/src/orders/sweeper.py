"""Re-settle orders stranded in `pending` (docs/contracts/services.md, "Settling a checkout").

A checkout leaves its order `pending` when the settle transaction fails after the charge (e.g. no free database
connection under load) or the process dies in between: the customer may be charged, but nothing records it. Payments
answers every charge of an order the same way and settling is idempotent, so charging again and settling is safe even
if the original request is still finishing.

Each tick claims up to `batch` `pending` orders untouched for `stale_after_s` (a lease, see `claim_stranded_orders`),
charges each again through the checkout's own client (same retries, same circuit breaker: an open circuit ends the
tick instead of adding load to a failing payments), and settles the answer: succeeded -> `paid`, declined -> `failed`.
No answer leaves the order `pending` for a later tick. Every orders replica runs one; their claims never overlap.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

from opentelemetry import metrics
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orders.payments_client import PaymentsClient
from orders.repository import claim_stranded_orders, oldest_pending_age_s, settle_order

log = logging.getLogger("orders")


class Sweeper:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        payments: PaymentsClient,
        *,
        interval_s: float = 10.0,
        stale_after_s: float = 30.0,
        batch: int = 50,
        meter: metrics.Meter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._payments = payments
        self.interval_s = interval_s
        self.stale_after_s = stale_after_s
        self.batch = batch
        self._sleep = sleep
        self._oldest_pending_age_s = 0.0
        meter = meter or metrics.get_meter("orders")
        self._recovered = meter.create_counter(
            "orders.settle.recovered",
            unit="{order}",
            description="Stranded pending orders settled by the sweeper, by resulting order status",
        )
        meter.create_observable_gauge(
            "orders.pending.oldest_age_seconds",
            unit="s",
            callbacks=[self._observe_oldest_pending_age],
            description="Age of the oldest pending order, as of the last sweep",
        )

    async def sweep_once(self) -> int:
        """One tick. Returns how many orders it settled."""
        settled = 0
        for order_id, total in await claim_stranded_orders(self._sessionmaker, self.stale_after_s, self.batch):
            permit = self._payments.breaker.allow()  # not admit(): that counts checkouts refused with 503
            if permit is None:
                log.info("stranded-order sweep paused: payments circuit open")
                break
            outcome = await self._payments.charge(order_id, total, permit)
            if outcome.error is not None:
                continue  # no answer: still pending, retried once the lease expires
            result = await settle_order(self._sessionmaker, order_id, total, outcome)
            if result.settled_now:
                settled += 1
                self._recovered.add(1, {"status": result.status})
                log.warning(
                    "stranded order settled",
                    extra={"order_id": order_id, "order_status": result.status, "payment_status": outcome.status},
                )
        self._oldest_pending_age_s = await oldest_pending_age_s(self._sessionmaker)
        return settled

    async def run(self) -> None:
        """Sweep every `interval_s` until cancelled; a failed sweep (e.g. database down) is logged, not fatal."""
        while True:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("stranded-order sweep failed")
            await self._sleep(self.interval_s)

    def _observe_oldest_pending_age(self, _options: metrics.CallbackOptions) -> Iterable[metrics.Observation]:
        yield metrics.Observation(self._oldest_pending_age_s)
