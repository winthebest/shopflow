"""Write shipments with the privileges of role fulfillment_worker: INSERT on shipments, SELECT on orders.id."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fulfillment_worker.events import OrderChange

# Untargeted ON CONFLICT DO NOTHING: a conflict target would need SELECT on shipments.order_id. order_id is the only
# unique constraint that can fire (the identity key never collides), so this is idempotent per order.
_INSERT = text(
    """
    INSERT INTO shipments (order_id, cdc_epoch, source_lsn)
    SELECT * FROM unnest(CAST(:order_ids AS bigint[]), CAST(:epochs AS integer[]), CAST(:lsns AS bigint[]))
    ON CONFLICT DO NOTHING
    """
)
_EXISTING_ORDERS = text("SELECT id FROM orders WHERE id = ANY(CAST(:ids AS bigint[]))")


@dataclass(frozen=True)
class BatchResult:
    created: int = 0
    duplicates: int = 0  # shipment already existed: re-delivery, rebalance or re-snapshot
    missing_orders: int = 0  # order not in Postgres (event from a life lost in a restore): never ship it


async def create_shipments(engine: AsyncEngine, changes: Sequence[OrderChange]) -> BatchResult:
    """One transaction: a shipment for each order that still exists and has none yet."""
    first_change: dict[int, OrderChange] = {}
    for change in changes:
        first_change.setdefault(change.order_id, change)
    if not first_change:
        return BatchResult()

    async with engine.begin() as conn:
        existing = set(await conn.scalars(_EXISTING_ORDERS, {"ids": list(first_change)}))
        rows = [change for order_id, change in first_change.items() if order_id in existing]
        created = 0
        if rows:
            result = await conn.execute(
                _INSERT,
                {
                    "order_ids": [row.order_id for row in rows],
                    "epochs": [row.epoch for row in rows],
                    "lsns": [row.lsn for row in rows],
                },
            )
            created = result.rowcount
    return BatchResult(created, len(rows) - created, len(first_change) - len(rows))
