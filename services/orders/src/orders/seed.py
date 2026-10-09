"""`seed` entrypoint: demo products and customers, idempotent (re-running inserts nothing new).

On a fresh database the ids are 1..len(PRODUCTS) and 1..CUSTOMER_COUNT, which loadtest/checkout.js relies on.
"""

import asyncio
import logging
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert

from orders.db import create_engine
from orders.models import Customer, Product
from orders.settings import Settings
from shopflow_common.log import configure_logging

CUSTOMER_COUNT = 100

PRODUCTS = [
    ("SKU-0001", "Espresso beans 1kg", "24.90"),
    ("SKU-0002", "Filter coffee 500g", "12.50"),
    ("SKU-0003", "Green tea 100 bags", "8.90"),
    ("SKU-0004", "Ceramic mug", "9.00"),
    ("SKU-0005", "French press 1L", "34.00"),
    ("SKU-0006", "Pour-over kettle", "45.00"),
    ("SKU-0007", "Burr grinder", "89.00"),
    ("SKU-0008", "Paper filters x100", "4.50"),
    ("SKU-0009", "Milk frother", "29.90"),
    ("SKU-0010", "Travel tumbler", "19.90"),
    ("SKU-0011", "Cold brew bottle", "22.00"),
    ("SKU-0012", "Digital scale", "27.50"),
    ("SKU-0013", "Dark chocolate 200g", "6.40"),
    ("SKU-0014", "Almond biscotti", "5.20"),
    ("SKU-0015", "Oat milk 1L", "3.10"),
    ("SKU-0016", "Matcha 50g", "18.00"),
    ("SKU-0017", "Chai spice mix", "7.80"),
    ("SKU-0018", "Glass carafe", "16.00"),
    ("SKU-0019", "Barista apron", "24.00"),
    ("SKU-0020", "Gift card", "50.00"),
]

log = logging.getLogger("seed")


async def seed(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
            products = await conn.execute(
                insert(Product)
                .values([{"sku": sku, "name": name, "price": Decimal(price)} for sku, name, price in PRODUCTS])
                .on_conflict_do_nothing(index_elements=["sku"])
            )
            customers = await conn.execute(
                insert(Customer)
                .values(
                    [
                        {"email": f"customer{n:03d}@example.com", "name": f"Customer {n:03d}"}
                        for n in range(1, CUSTOMER_COUNT + 1)
                    ]
                )
                .on_conflict_do_nothing(index_elements=["email"])
            )
        log.info("seeded", extra={"products_inserted": products.rowcount, "customers_inserted": customers.rowcount})
    finally:
        await engine.dispose()


def main() -> None:
    settings = Settings()
    configure_logging("orders", settings.log_level)
    asyncio.run(seed(settings.database_url.get_secret_value()))
