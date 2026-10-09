"""Checkout request shared by gateway (validates at the edge) and orders (validates again before writing)."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Postgres bigint upper bound: larger ids would fail in the driver instead of returning 422.
MAX_ID = 2**63 - 1
MAX_ITEMS = 50
MAX_QUANTITY = 100


class CheckoutItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0, le=MAX_ID)
    quantity: int = Field(ge=1, le=MAX_QUANTITY)


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: int = Field(gt=0, le=MAX_ID)
    items: list[CheckoutItem] = Field(min_length=1, max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def _unique_products(self) -> "CheckoutRequest":
        product_ids = [item.product_id for item in self.items]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("each product_id may appear only once; use quantity instead")
        return self
