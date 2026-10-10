"""Consume loop: at-least-once, idempotent writes.

Per batch: parse -> create shipments in one transaction -> commit the Kafka offsets. Offsets are committed only after
the database commit, so a crash re-delivers the batch and `ON CONFLICT DO NOTHING` absorbs the duplicates.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from aiokafka.errors import CommitFailedError
from opentelemetry import trace
from sqlalchemy.exc import DBAPIError

from fulfillment_worker.events import MalformedEventError, OrderChange, parse_order_change
from fulfillment_worker.store import BatchResult

POLL_TIMEOUT_MS = 1_000
# Backoff 0.5, 1, 2, 4, 8, 10s (~25s): rides out a CNPG switchover; well inside max.poll.interval (300s).
STORE_ATTEMPTS = 7
MAX_BACKOFF_S = 10.0

log = logging.getLogger("worker")
tracer = trace.get_tracer("fulfillment_worker")


class Consumer(Protocol):
    """The part of aiokafka.AIOKafkaConsumer the loop uses (a fake in unit tests)."""

    async def getmany(self, *partitions: object, timeout_ms: int, max_records: int | None) -> dict: ...

    async def commit(self) -> None: ...


Store = Callable[[Sequence[OrderChange]], Awaitable[BatchResult]]


class Worker:
    def __init__(
        self,
        consumer: Consumer,
        store: Store,
        *,
        max_records: int,
        shipment_latency_s: float,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.consumer = consumer
        self.store = store
        self.max_records = max_records
        self.shipment_latency_s = shipment_latency_s
        self.sleep = sleep or self._pause
        self.last_poll = time.monotonic()
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Finish the batch in progress (cutting short its simulated latency), commit it, then leave run()."""
        self._stopping.set()

    def seconds_since_poll(self) -> float:
        return time.monotonic() - self.last_poll

    async def run(self) -> None:
        try:
            while not self._stopping.is_set():
                await self.process_once()
        except Exception:
            log.exception("consume loop crashed; the pod restarts from the last committed offset")
            raise

    async def _pause(self, seconds: float) -> None:
        """Sleep that ends early on stop(), so shutdown never waits for simulated latency."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def process_once(self) -> int:
        """Poll one batch and handle it. Returns the number of records consumed."""
        batches = await self.consumer.getmany(timeout_ms=POLL_TIMEOUT_MS, max_records=self.max_records)
        self.last_poll = time.monotonic()
        records = [record for partition_records in batches.values() for record in partition_records]
        if not records:
            return 0

        with tracer.start_as_current_span(
            "fulfillment batch", attributes={"messaging.batch.message_count": len(records)}
        ):
            changes, malformed = [], 0
            for record in records:
                try:
                    changes.append(parse_order_change(record.value))
                except MalformedEventError as exc:
                    malformed += 1  # poison records must not block the partition
                    log.warning("skipping malformed record", extra={"offset": record.offset, "error": str(exc)})
            wanted = [change for change in changes if change.needs_shipment]

            result = await self._store_with_retry(wanted)
            if result.created and self.shipment_latency_s:
                await self.sleep(result.created * self.shipment_latency_s)  # simulated carrier booking
            try:
                await self.consumer.commit()
            except CommitFailedError as exc:
                # The group rebalanced mid-batch (KEDA scaling, rollout): the partitions' new owner re-reads this
                # batch and ON CONFLICT DO NOTHING absorbs it. Keep consuming instead of crashing into a restart.
                log.warning("offset commit lost to a rebalance; batch will be re-delivered", extra={"error": str(exc)})

            log.info(
                "batch done",
                extra={
                    "records": len(records),
                    "paid_changes": len(wanted),
                    "shipments_created": result.created,
                    "duplicates": result.duplicates,
                    "missing_orders": result.missing_orders,
                    "malformed": malformed,
                    "cdc_epochs": sorted({change.epoch for change in changes}),
                },
            )
            if result.missing_orders:
                log.warning("orders not found in Postgres, not shipped", extra={"count": result.missing_orders})
        return len(records)

    async def _store_with_retry(self, wanted: Sequence[OrderChange]) -> BatchResult:
        if not wanted:
            return BatchResult()
        for attempt in range(1, STORE_ATTEMPTS + 1):
            try:
                return await self.store(wanted)
            except (DBAPIError, OSError, TimeoutError) as exc:
                if attempt == STORE_ATTEMPTS:
                    raise  # task dies -> /healthz fails -> restart from the last committed offset
                delay = min(0.5 * 2 ** (attempt - 1), MAX_BACKOFF_S)
                log.warning("store failed, retrying", extra={"attempt": attempt, "delay_s": delay, "error": str(exc)})
                await self.sleep(delay)
        raise AssertionError("unreachable")
