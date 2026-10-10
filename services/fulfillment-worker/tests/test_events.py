"""Parsing `shop.public.orders` records (format: deploy/platform/kafka-connect/base/connectors.yaml, ADR 0405)."""

import json

import pytest

from fulfillment_worker.events import MalformedEventError, OrderChange, parse_order_change


def record(**fields) -> bytes:
    """An after-image as the connector writes it: row columns + _op/_lsn/_source_ts_ms/_cdc_epoch."""
    value = {
        "id": 42,
        "customer_id": 7,
        "status": "paid",
        "total": "58.80",
        "created_at": "2026-10-10T06:00:00.123456Z",
        "updated_at": "2026-10-10T06:00:00.234567Z",
        "_op": "u",
        "_lsn": 26_843_216,
        "_source_ts_ms": 1_791_612_000_234,
        "_cdc_epoch": "3",
    } | fields
    return json.dumps({k: v for k, v in value.items() if v is not ...}).encode()


@pytest.mark.parametrize(
    ("fields", "ships"),
    [
        ({"_op": "u", "status": "paid"}, True),  # pending -> paid, the normal path
        ({"_op": "c", "status": "paid"}, True),
        ({"_op": "r", "status": "paid"}, True),  # snapshot read after a new Kafka life / re-snapshot
        ({"_op": "c", "status": "pending"}, False),
        ({"_op": "u", "status": "failed"}, False),
        ({"_op": "r", "status": "pending"}, False),
        (
            {"_op": "d", "status": ..., "__deleted": "true"},
            False,
        ),  # delete: only the key under replica identity default
    ],
    ids=[
        "update-paid",
        "create-paid",
        "snapshot-paid",
        "create-pending",
        "update-failed",
        "snapshot-pending",
        "delete",
    ],
)
def test_which_changes_need_a_shipment(fields, ships):
    assert parse_order_change(record(**fields)).needs_shipment is ships


def test_fields_are_typed():
    assert parse_order_change(record()) == OrderChange(order_id=42, status="paid", op="u", lsn=26_843_216, epoch=3)


def test_deleted_flag_wins_even_if_status_looks_paid():
    change = parse_order_change(record(_op="u", status="paid", __deleted="true"))
    assert change.deleted
    assert not change.needs_shipment


def test_missing_lsn_defaults_to_zero():
    assert parse_order_change(record(_lsn=None)).lsn == 0


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"not json",
        b"[1, 2]",
        record(_op=...),
        record(id=...),
        record(_cdc_epoch=...),
        record(id="abc"),
        record(_cdc_epoch="three"),
    ],
    ids=["tombstone", "not-json", "not-object", "no-op", "no-id", "no-epoch", "bad-id", "bad-epoch"],
)
def test_malformed_records_are_rejected(value):
    with pytest.raises(MalformedEventError):
        parse_order_change(value)
