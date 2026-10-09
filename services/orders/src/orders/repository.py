"""Database operations for checkout. The payments call happens between two short transactions, never inside one."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orders.models import Customer, Order, OrderItem, Payment, Product
from orders.payments_client import ChargeOutcome
from shopflow_common.schemas import CheckoutRequest

# The demo catalog is small; the cap keeps the endpoint bounded if it grows.
PRODUCTS_LIMIT = 100


class UnknownReferenceError(Exception):
    """The request names a customer or product that does not exist."""


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


async def list_products(session: AsyncSession) -> list[ProductOut]:
    products = await session.scalars(select(Product).order_by(Product.id).limit(PRODUCTS_LIMIT))
    return [ProductOut.model_validate(p) for p in products]


async def create_pending_order(
    sessionmaker: async_sessionmaker[AsyncSession], req: CheckoutRequest
) -> tuple[int, Decimal]:
    """Insert the order (`pending`) and its items in one transaction, priced from the catalog."""
    async with sessionmaker.begin() as session:
        if await session.get(Customer, req.customer_id) is None:
            raise UnknownReferenceError(f"unknown customer {req.customer_id}")

        product_ids = [item.product_id for item in req.items]
        rows = await session.execute(select(Product.id, Product.price).where(Product.id.in_(product_ids)))
        prices: dict[int, Decimal] = dict(rows.all())
        missing = sorted(set(product_ids) - prices.keys())
        if missing:
            raise UnknownReferenceError(f"unknown products {missing}")

        total = sum((prices[item.product_id] * item.quantity for item in req.items), Decimal(0))
        order = Order(customer_id=req.customer_id, status="pending", total=total)
        session.add(order)
        await session.flush()  # assigns order.id
        session.add_all(
            OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=prices[item.product_id],
            )
            for item in req.items
        )
        return order.id, total


async def settle_order(
    sessionmaker: async_sessionmaker[AsyncSession], order_id: int, amount: Decimal, outcome: ChargeOutcome
) -> str:
    """Record the charge outcome and move the order `pending -> paid | failed`. Returns the new status."""
    status = "paid" if outcome.status == "succeeded" else "failed"
    async with sessionmaker.begin() as session:
        await session.execute(
            update(Order).where(Order.id == order_id, Order.status == "pending").values(status=status)
        )
        session.add(Payment(order_id=order_id, amount=amount, status=outcome.status, provider_ref=outcome.charge_id))
    return status


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
