"""Format contract: real `shop.public.orders` values captured by sf-data on k3d (Connect image sha-cb4eded,
2026-10-10, no personal data). If the connector's SMT chain changes shape, these fail before the cluster does."""

from pathlib import Path

from fulfillment_worker.events import OrderChange, parse_order_change

FIXTURES = Path(__file__).parent / "fixtures" / "shop-public-orders.jsonl"
EPOCH = 1_791_611_151  # _cdc_epoch is a string in the record (InsertField static value)


def records() -> list[bytes]:
    return [line.encode() for line in FIXTURES.read_text().splitlines() if line.strip()]


def test_real_create_update_delete():
    create, update, delete = (parse_order_change(value) for value in records())

    assert create == OrderChange(order_id=1, status="pending", op="c", lsn=201_440_576, epoch=EPOCH)
    assert not create.needs_shipment
    assert update == OrderChange(order_id=1, status="paid", op="u", lsn=201_457_248, epoch=EPOCH)
    assert update.needs_shipment
    # Debezium fills a delete's columns with defaults (status "", customer 0), not nulls: only _op tells.
    assert (delete.op, delete.deleted) == ("d", True)
    assert not delete.needs_shipment


def test_snapshot_read_of_a_paid_order_ships():
    # No real `r` for orders yet (orders was empty at snapshot time); same shape as `u` with _op "r" and the
    # snapshot LSN, per sf-data. Shipping it keeps "every paid order has one shipment" across re-snapshots.
    update = records()[1].decode()
    snapshot = parse_order_change(update.replace('"_op":"u"', '"_op":"r"').replace("201457248", "100669536").encode())
    assert snapshot.op == "r"
    assert snapshot.needs_shipment
