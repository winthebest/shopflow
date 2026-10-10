"""Consume loop semantics with a fake consumer and store: commit only after a successful store, retries, poison."""

import json
from collections import namedtuple

import pytest
from sqlalchemy.exc import OperationalError

from fulfillment_worker.events import OrderChange
from fulfillment_worker.store import BatchResult
from fulfillment_worker.worker import STORE_ATTEMPTS, Worker

Record = namedtuple("Record", "offset value")


def change(order_id: int, status: str = "paid", op: str = "u") -> bytes:
    return json.dumps({"id": order_id, "status": status, "_op": op, "_lsn": 100 + order_id, "_cdc_epoch": "1"}).encode()


class FakeConsumer:
    def __init__(self, *batches: list[bytes | None]) -> None:
        self.batches = [{"tp": [Record(i, v) for i, v in enumerate(batch)]} for batch in batches]
        self.commits = 0

    async def getmany(self, *partitions, timeout_ms: int, max_records: int | None) -> dict:
        return self.batches.pop(0) if self.batches else {}

    async def commit(self) -> None:
        self.commits += 1


class FakeStore:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[list[OrderChange]] = []
        self.failures = failures

    async def __call__(self, changes) -> BatchResult:
        self.calls.append(list(changes))
        if self.failures:
            self.failures -= 1
            raise OperationalError("INSERT", {}, ConnectionResetError("db went away"))
        return BatchResult(created=len(changes))


async def no_sleep(seconds: float) -> None:
    no_sleep.total += seconds


def worker(consumer, store, latency_s: float = 0.0) -> Worker:
    no_sleep.total = 0.0
    return Worker(consumer, store, max_records=500, shipment_latency_s=latency_s, sleep=no_sleep)


async def test_paid_changes_are_stored_then_offsets_committed():
    consumer, store = FakeConsumer([change(1), change(2, "pending"), change(3, op="r")]), FakeStore()

    assert await worker(consumer, store).process_once() == 3

    assert [c.order_id for c in store.calls[0]] == [1, 3]  # pending skipped, snapshot read kept
    assert consumer.commits == 1


async def test_batch_without_paid_changes_is_committed_without_touching_the_db():
    consumer, store = FakeConsumer([change(1, "pending"), change(2, "failed")]), FakeStore()
    await worker(consumer, store).process_once()
    assert store.calls == []
    assert consumer.commits == 1


async def test_empty_poll_commits_nothing():
    consumer = FakeConsumer()
    assert await worker(consumer, FakeStore()).process_once() == 0
    assert consumer.commits == 0


async def test_malformed_records_are_skipped_not_blocking(caplog):
    consumer, store = FakeConsumer([b"garbage", None, change(5)]), FakeStore()
    await worker(consumer, store).process_once()
    assert [c.order_id for c in store.calls[0]] == [5]
    assert consumer.commits == 1
    assert sum(r.message == "skipping malformed record" for r in caplog.records) == 2


async def test_transient_db_error_is_retried_before_committing():
    consumer, store = FakeConsumer([change(1)]), FakeStore(failures=2)
    await worker(consumer, store).process_once()
    assert len(store.calls) == 3
    assert consumer.commits == 1


async def test_persistent_db_error_kills_the_loop_without_committing():
    consumer, store = FakeConsumer([change(1)]), FakeStore(failures=STORE_ATTEMPTS)
    with pytest.raises(OperationalError):
        await worker(consumer, store).process_once()
    assert len(store.calls) == STORE_ATTEMPTS
    assert consumer.commits == 0  # the batch is re-delivered after the restart


async def test_simulated_carrier_latency_is_per_created_shipment():
    consumer = FakeConsumer([change(1), change(2), change(3)])
    await worker(consumer, FakeStore(), latency_s=0.05).process_once()
    assert no_sleep.total == pytest.approx(0.15)


async def test_stop_ends_the_loop_after_the_current_batch():
    consumer, store = FakeConsumer([change(1)], [change(2)]), FakeStore()
    loop = worker(consumer, store)

    original = loop.process_once

    async def process_then_stop() -> int:
        consumed = await original()
        loop.stop()
        return consumed

    loop.process_once = process_then_stop
    await loop.run()
    assert [c.order_id for call in store.calls for c in call] == [1]
    assert consumer.commits == 1
