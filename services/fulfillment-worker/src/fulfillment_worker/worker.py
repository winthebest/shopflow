"""Consume loop: at-least-once, idempotent writes.

Per batch: parse -> create shipments in one transaction -> commit the Kafka offsets. Offsets are committed only after
the database commit, so a crash re-delivers the batch and `ON CONFLICT DO NOTHING` absorbs the duplicates.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from opentelemetry import trace
from sqlalchemy.exc import DBAPIError

from fulfillment_worker.events import MalformedEventError, OrderChange, parse_order_change
from fulfillment_worker.store import BatchResult

POLL_TIMEOUT_MS = 1_000
STORE_ATTEMPTS = 4  # ~7s of backoff in total; well inside the consumer's max.poll.interval (300s)

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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.consumer = consumer
        self.store = store
        self.max_records = max_records
        self.shipment_latency_s = shipment_latency_s
        self.sleep = sleep
        self.last_poll = time.monotonic()
        self._stopping = False

    def stop(self) -> None:
        """Finish the batch in progress, commit it, then return from run()."""
        self._stopping = True

    async def run(self) -> None:
        while not self._stopping:
            await self.process_once()

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
            await self.consumer.commit()

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
                delay = 0.5 * 2 ** (attempt - 1)
                log.warning("store failed, retrying", extra={"attempt": attempt, "delay_s": delay, "error": str(exc)})
                await self.sleep(delay)
        raise AssertionError("unreachable")
