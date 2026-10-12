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


def test_fingerprint_ignores_key_order_but_not_items():
    from shopflow_common.schemas import request_fingerprint

    a = CheckoutRequest.model_validate_json('{"customer_id": 1, "items": [{"product_id": 2, "quantity": 1}]}')
    b = CheckoutRequest.model_validate_json('{"items": [{"quantity": 1, "product_id": 2}], "customer_id": 1}')
    c = CheckoutRequest.model_validate_json('{"customer_id": 1, "items": [{"product_id": 2, "quantity": 2}]}')
    assert request_fingerprint(a) == request_fingerprint(b)
    assert request_fingerprint(a) != request_fingerprint(c)
    assert len(request_fingerprint(a)) == 64


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("06ac23a4-89e3-461c-a0d2-12a98dda0386", True),
        ("x" * 64, True),
        ("x" * 65, False),
        ("", False),
        ("has space", False),
        ("tab\there", False),
    ],
)
def test_idempotency_key_pattern(key, valid):
    import re

    from shopflow_common.schemas import IDEMPOTENCY_KEY_PATTERN

    assert bool(re.fullmatch(IDEMPOTENCY_KEY_PATTERN, key)) is valid
