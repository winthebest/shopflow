"""ORM models for the shop schema.

Alembic (services/orders/migrations) is the only owner of the schema: these models must match the migrations, which
the integration tests enforce with `alembic check`. Every table has a bigint identity primary key and `created_at` /
`updated_at`; `updated_at` is maintained by a database trigger so any writer keeps it correct (CDC and the
reconciliation cutoff in later phases rely on it).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Numeric(12, 2)

ORDER_STATUSES = ("pending", "paid", "failed")
PAYMENT_STATUSES = ("succeeded", "declined", "error")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class StandardColumns:
    """Columns every shop table has: bigint identity key first, timestamps last."""

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True, sort_order=-1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), sort_order=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), sort_order=1)


class Product(StandardColumns, Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("price > 0", name="price_positive"),)

    sku: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Money)


class Customer(StandardColumns, Base):
    __tablename__ = "customers"

    email: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)


class Order(StandardColumns, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_in("status", ORDER_STATUSES), name="status_valid"),
        CheckConstraint("total >= 0", name="total_non_negative"),
    )

    customer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(Text)
    total: Mapped[Decimal] = mapped_column(Money)


class OrderItem(StandardColumns, Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
    )

    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Money)


class Payment(StandardColumns, Base):
    """Outcome of the one charge attempt per order (`declined` = provider said no, `error` = no answer)."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_in("status", PAYMENT_STATUSES), name="status_valid"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
    )

    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), unique=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(Text)


class Heartbeat(Base):
    """Single row Debezium updates every 10s (`heartbeat.action.query`), so the replication slot advances and
    freshness is measurable even when the shop is idle. Published to CDC; spec in docs/contracts/services.md."""

    __tablename__ = "heartbeat"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CdcEpoch(Base):
    """One row per Debezium (re)snapshot, written by scripts/cdc-epoch.sh (sf-data). Never published."""

    __tablename__ = "cdc_epochs"
    __table_args__ = ({"schema": "meta"},)

    epoch: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    snapshot_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
