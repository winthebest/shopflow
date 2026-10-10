"""Write shipments with the privileges of role fulfillment_worker: INSERT on shipments, SELECT on orders (id, status).

A change event says an order *was* paid; Postgres says whether it *is* paid. Shipping only orders that are paid now
keeps the stream safe after a PITR restore: an old `paid` event for an order that was lost, or restored as `pending`
(its payment fell in the RPO window), ships nothing; when that order is paid again, the new `u` event ships it.
Statuses are terminal (pending -> paid | failed), so the check is exact. If an id was reused by a new order that is
also paid, it still gets exactly one shipment; only its cdc_epoch/source_lsn point at the older event.
"""

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
_PAID_ORDERS = text("SELECT id FROM orders WHERE id = ANY(CAST(:ids AS bigint[])) AND status = 'paid'")


@dataclass(frozen=True)
class BatchResult:
    created: int = 0
    duplicates: int = 0  # shipment already existed: re-delivery, rebalance or re-snapshot
    not_paid_now: int = 0  # order missing, or not paid, in Postgres (e.g. after a restore): never ship it


async def create_shipments(engine: AsyncEngine, changes: Sequence[OrderChange]) -> BatchResult:
    """One transaction: a shipment for each order that is paid in Postgres now and has none yet."""
    first_change: dict[int, OrderChange] = {}
    for change in changes:
        first_change.setdefault(change.order_id, change)
    if not first_change:
        return BatchResult()

    async with engine.begin() as conn:
        paid_now = set(await conn.scalars(_PAID_ORDERS, {"ids": list(first_change)}))
        rows = [change for order_id, change in first_change.items() if order_id in paid_now]
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
