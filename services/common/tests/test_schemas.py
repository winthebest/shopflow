import pytest
from pydantic import ValidationError

from shopflow_common.schemas import MAX_ID, CheckoutRequest


def test_valid_checkout():
    req = CheckoutRequest.model_validate({"customer_id": 1, "items": [{"product_id": 2, "quantity": 3}]})
    assert req.items[0].quantity == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"customer_id": 1, "items": []},
        {"customer_id": 0, "items": [{"product_id": 1, "quantity": 1}]},
        {"customer_id": MAX_ID + 1, "items": [{"product_id": 1, "quantity": 1}]},
        {"customer_id": 1, "items": [{"product_id": 1, "quantity": 0}]},
        {"customer_id": 1, "items": [{"product_id": 1, "quantity": 101}]},
        {"customer_id": 1, "items": [{"product_id": 1, "quantity": 1}, {"product_id": 1, "quantity": 2}]},
        {"customer_id": 1, "items": [{"product_id": 1, "quantity": 1}], "discount": 10},
        {"customer_id": 1, "items": [{"product_id": i, "quantity": 1} for i in range(1, 52)]},
    ],
    ids=["no-items", "zero-customer", "id-overflow", "zero-qty", "qty-too-big", "dup-product", "extra", "too-many"],
)
def test_invalid_checkout(payload):
    with pytest.raises(ValidationError):
        CheckoutRequest.model_validate(payload)
