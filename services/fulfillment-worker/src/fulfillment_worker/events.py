"""Records of `shop.public.orders` as the Debezium connector writes them (deploy/platform/kafka-connect, ADR 0405).

Value = after-image as JSON without a schema envelope, plus `_op` (c|u|d|r), `_lsn`, `_source_ts_ms` and
`_cdc_epoch`; deletes are rewritten to a row with `_op = 'd'` (and only the key, under REPLICA IDENTITY DEFAULT).
"""

import json
from dataclasses import dataclass

# Snapshot reads (`r`) count too: after a new Kafka life, a restore or a re-snapshot, paid orders arrive only as `r`.
# UNIQUE(order_id) makes re-processing them a no-op, so every paid order still gets exactly one shipment.
SHIPPING_OPS = frozenset({"c", "u", "r"})


class MalformedEventError(ValueError):
    """The record is not an order change in the agreed format."""


@dataclass(frozen=True)
class OrderChange:
    order_id: int
    status: str | None
    op: str
    lsn: int
    epoch: int
    # A delete carries defaults, not nulls, in its non-key columns (status "", customer_id 0, 1970 timestamps) and
    # `__deleted: "true"`: decide on _op/__deleted, never on status.
    deleted: bool = False

    @property
    def needs_shipment(self) -> bool:
        return self.op in SHIPPING_OPS and not self.deleted and self.status == "paid"


def parse_order_change(value: bytes | None) -> OrderChange:
    if value is None:
        raise MalformedEventError("empty record (tombstone)")
    try:
        data = json.loads(value)
    except ValueError as exc:
        raise MalformedEventError(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedEventError("not a JSON object")
    try:
        op = str(data["_op"])
        order_id = int(data["id"])
        epoch = int(data["_cdc_epoch"])  # the InsertField SMT writes it as a string
        lsn = int(data.get("_lsn") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedEventError(f"missing or invalid field: {exc}") from exc
    status = data.get("status")
    deleted = op == "d" or str(data.get("__deleted", "false")).lower() == "true"
    return OrderChange(order_id, status if isinstance(status, str) else None, op, lsn, epoch, deleted)
