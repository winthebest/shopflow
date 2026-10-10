"""fulfillment-worker end to end: Kafka API (Redpanda) + the real shop schema, as role fulfillment_worker.

Lives next to the orders integration fixtures (CNPG-like Postgres with every role) instead of duplicating them.
"""

import asyncio
import json
import time

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from sqlalchemy import text
from testcontainers.community.kafka import RedpandaContainer

from fulfillment_worker.main import create_app
from fulfillment_worker.settings import Settings
from orders.db import create_engine

pytestmark = pytest.mark.integration

REDPANDA_IMAGE = (
    "docker.redpanda.com/redpandadata/redpanda:v26.2.2"
    "@sha256:468bd13a9f2bd24794cb7fddc867c767fb1008b9a07b297b89fde48c564d7d96"
)
TOPIC = "shop.public.orders"
GROUP = "fulfillment-worker-test"
MISSING_ORDER = 999_999


@pytest.fixture(scope="module")
def bootstrap() -> str:
    with RedpandaContainer(REDPANDA_IMAGE) as redpanda:
        yield redpanda.get_bootstrap_server()


def debezium(order_id: int, status: str, op: str, epoch: int, lsn: int) -> bytes:
    """Same shape as the shop-postgres connector output (after-image + _op/_lsn/_source_ts_ms/_cdc_epoch)."""
    return json.dumps(
        {"id": order_id, "status": status, "total": "9.90", "_op": op, "_lsn": lsn, "_cdc_epoch": str(epoch)}
    ).encode()


async def produce(bootstrap: str, records: list[tuple[int, bytes]]) -> None:
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    await producer.start()
    try:
        for key, value in records:
            await producer.send_and_wait(TOPIC, value=value, key=json.dumps({"id": key}).encode())
    finally:
        await producer.stop()


async def ensure_topic(bootstrap: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        if TOPIC not in await admin.list_topics():
            await admin.create_topics([NewTopic(TOPIC, num_partitions=3, replication_factor=1)])
    finally:
        await admin.close()


async def caught_up(bootstrap: str) -> bool:
    """The worker's group has committed every record of the topic."""
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    probe = AIOKafkaConsumer(bootstrap_servers=bootstrap)
    await admin.start()
    await probe.start()
    try:
        partitions = [TopicPartition(TOPIC, p) for p in probe.partitions_for_topic(TOPIC) or ()]
        if not partitions:
            await probe.topics()  # refresh metadata once
            partitions = [TopicPartition(TOPIC, p) for p in probe.partitions_for_topic(TOPIC) or ()]
        ends = await probe.end_offsets(partitions)
        committed = await admin.list_consumer_group_offsets(GROUP)
        return all(committed.get(tp) is not None and committed[tp].offset >= end for tp, end in ends.items())
    finally:
        await probe.stop()
        await admin.close()


async def shipments(url: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT order_id, cdc_epoch, source_lsn FROM shipments ORDER BY order_id"))
            return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


async def run_worker_until_caught_up(bootstrap: str, worker_url: str, timeout_s: float = 60) -> None:
    settings = Settings(
        kafka_bootstrap_servers=bootstrap,
        kafka_security_protocol="PLAINTEXT",
        kafka_group_id=GROUP,
        database_url=worker_url,
        shipment_latency_ms=0,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        deadline = time.monotonic() + timeout_s
        while not await caught_up(bootstrap):
            assert not app.state.task.done(), app.state.task.exception()
            assert time.monotonic() < deadline, "worker did not catch up with the topic"
            await asyncio.sleep(0.5)


async def new_order(url: str, status: str) -> int:
    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            row = await conn.execute(
                text("INSERT INTO orders (customer_id, status, total) VALUES (1, :status, 9.90) RETURNING id"),
                {"status": status},
            )
            return row.scalar_one()
    finally:
        await engine.dispose()


async def set_status(url: str, order_id: int, status: str) -> None:
    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE orders SET status = :status WHERE id = :id"), {"status": status, "id": order_id}
            )
    finally:
        await engine.dispose()


async def test_one_shipment_per_order_paid_now_even_after_a_restore_and_a_re_snapshot(seeded_db, worker_url, bootstrap):
    paid_a, pending_b, paid_c = (
        await new_order(seeded_db, "paid"),
        await new_order(seeded_db, "pending"),
        await new_order(seeded_db, "paid"),
    )
    # Restored order: Kafka still has its old `paid` event, but the restore brought it back as `pending`.
    restored_d = await new_order(seeded_db, "pending")
    await ensure_topic(bootstrap)

    # Epoch 1: the checkout stream, an order Postgres no longer has, the restored order's old event, a poison record.
    await produce(
        bootstrap,
        [
            (paid_a, debezium(paid_a, "pending", "c", 1, 100)),
            (paid_a, debezium(paid_a, "paid", "u", 1, 110)),
            (pending_b, debezium(pending_b, "pending", "c", 1, 120)),
            (paid_c, debezium(paid_c, "pending", "c", 1, 130)),
            (paid_c, debezium(paid_c, "paid", "u", 1, 140)),
            (MISSING_ORDER, debezium(MISSING_ORDER, "paid", "u", 1, 150)),
            (restored_d, debezium(restored_d, "paid", "u", 1, 160)),
            (0, b"not json"),
        ],
    )
    await run_worker_until_caught_up(bootstrap, worker_url)
    assert await shipments(seeded_db) == [(paid_a, 1, 110), (paid_c, 1, 140)]  # nothing for missing or restored

    # The restored order is paid again: its new `u` event ships it, exactly once.
    await set_status(seeded_db, restored_d, "paid")
    await produce(bootstrap, [(restored_d, debezium(restored_d, "paid", "u", 2, 700))])
    await run_worker_until_caught_up(bootstrap, worker_url)
    assert await shipments(seeded_db) == [(paid_a, 1, 110), (paid_c, 1, 140), (restored_d, 2, 700)]

    # Epoch 2: a re-snapshot replays every order as a snapshot read; a restart resumes from committed offsets.
    await produce(
        bootstrap,
        [
            (order_id, debezium(order_id, status, "r", 2, 900))
            for order_id, status in [(paid_a, "paid"), (pending_b, "pending"), (paid_c, "paid"), (restored_d, "paid")]
        ],
    )
    await run_worker_until_caught_up(bootstrap, worker_url)
    assert await shipments(seeded_db) == [(paid_a, 1, 110), (paid_c, 1, 140), (restored_d, 2, 700)]  # no duplicate
