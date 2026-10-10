"""Database operations for checkout. The payments call happens between two short transactions, never inside one.

Settling is idempotent: only the settle that moves the order out of `pending` writes the payment row, so the request
and anything that re-settles a stranded order can race on the same order safely.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orders.models import Customer, Order, OrderItem, Payment, Product
from orders.payments_client import ChargeOutcome
from shopflow_common.schemas import CheckoutRequest

# The demo catalog is small; the cap keeps the endpoint bounded if it grows.
PRODUCTS_LIMIT = 100
MAX_TOTAL = Decimal("9999999999.99")  # Numeric(12, 2)


class InvalidCheckoutError(Exception):
    """The request names a customer or product that does not exist, or its total does not fit the schema."""


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    price: Decimal


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    quantity: int
    unit_price: Decimal


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    status: str
    total: Decimal
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut]


@dataclass(frozen=True)
class PendingOrder:
    """What transaction 1 wrote. With the settle result it is the whole checkout answer: no read after settling."""

    id: int
    customer_id: int
    total: Decimal
    created_at: datetime
    items: list[OrderItemOut]


@dataclass(frozen=True)
class Settled:
    status: str  # the order's status after the settle transaction
    updated_at: datetime
    settled_now: bool  # False: the order had already left `pending` (another settle won); nothing was written


async def list_products(session: AsyncSession) -> list[ProductOut]:
    products = await session.scalars(select(Product).order_by(Product.id).limit(PRODUCTS_LIMIT))
    return [ProductOut.model_validate(p) for p in products]


async def create_pending_order(sessionmaker: async_sessionmaker[AsyncSession], req: CheckoutRequest) -> PendingOrder:
    """Insert the order (`pending`) and its items in one transaction, priced from the catalog."""
    async with sessionmaker.begin() as session:
        if await session.get(Customer, req.customer_id) is None:
            raise InvalidCheckoutError(f"unknown customer {req.customer_id}")

        product_ids = [item.product_id for item in req.items]
        rows = await session.execute(select(Product.id, Product.price).where(Product.id.in_(product_ids)))
        prices: dict[int, Decimal] = dict(rows.all())
        missing = sorted(set(product_ids) - prices.keys())
        if missing:
            raise InvalidCheckoutError(f"unknown products {missing}")

        total = sum((prices[item.product_id] * item.quantity for item in req.items), Decimal(0))
        if total > MAX_TOTAL:
            raise InvalidCheckoutError(f"order total {total} exceeds {MAX_TOTAL}")
        order_id, created_at = (
            await session.execute(
                insert(Order)
                .values(customer_id=req.customer_id, status="pending", total=total)
                .returning(Order.id, Order.created_at)
            )
        ).one()
        items = [
            OrderItemOut(product_id=item.product_id, quantity=item.quantity, unit_price=prices[item.product_id])
            for item in req.items
        ]
        session.add_all(OrderItem(order_id=order_id, **item.model_dump()) for item in items)
        return PendingOrder(order_id, req.customer_id, total, created_at, items)


async def settle_order(
    sessionmaker: async_sessionmaker[AsyncSession], order_id: int, amount: Decimal, outcome: ChargeOutcome
) -> Settled:
    """Move the order `pending -> paid | failed` and record the charge, once.

    The conditional UPDATE takes the row lock: a concurrent settle waits, then finds the order no longer `pending`
    and writes nothing (READ COMMITTED re-checks the WHERE on the new row version). It then reports the status the
    winner set. `UNIQUE(payments.order_id)` backs this up.
    """
    status = "paid" if outcome.status == "succeeded" else "failed"
    async with sessionmaker.begin() as session:
        won = (
            await session.execute(
                update(Order)
                .where(Order.id == order_id, Order.status == "pending")
                .values(status=status)
                .returning(Order.status, Order.updated_at)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if won is None:
            current = (await session.execute(select(Order.status, Order.updated_at).where(Order.id == order_id))).one()
            return Settled(current.status, current.updated_at, settled_now=False)
        session.add(Payment(order_id=order_id, amount=amount, status=outcome.status, provider_ref=outcome.charge_id))
        return Settled(won.status, won.updated_at, settled_now=True)


async def get_order(session: AsyncSession, order_id: int) -> OrderOut | None:
    order = await session.get(Order, order_id)
    if order is None:
        return None
    items = await session.scalars(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id))
    return OrderOut(
        id=order.id,
        customer_id=order.customer_id,
        status=order.status,
        total=order.total,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=[OrderItemOut.model_validate(item) for item in items],
    )
