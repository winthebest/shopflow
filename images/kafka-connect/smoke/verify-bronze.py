"""Check what the Iceberg sink committed during the smoke test, reading as the read-only principal.

Usage: verify-bronze.py <wait-seconds>
Connects to Polaris with the trino_lake_ro credentials written by polaris-setup.py and to SeaweedFS with the
read-only S3 identity, polls until the expected change events are committed or the wait runs out, then checks that
the read-only principal cannot write to the catalog. Exit code 0 = pass.
"""

import os
import sys
import time

from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import ForbiddenError

# Change events run.sh produces, per table: (primary key, _op). Snapshot rows arrive as 'r'.
EXPECTED = {
    "customers": {(1, "r"), (2, "r"), (3, "c")},
    "orders": {(1, "r"), (2, "c"), (2, "u"), (1, "d")},
}
METADATA_COLUMNS = ("_op", "_lsn", "_source_ts_ms", "_cdc_epoch", "_ingested_at")


def read_properties(path: str) -> dict[str, str]:
    with open(path) as f:
        return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)


def catalog() -> RestCatalog:
    creds = read_properties("/work/secrets/lakehouse/polaris-trino-lake-ro.properties")
    return RestCatalog(
        "lake_ro",
        uri=f"{os.environ['POLARIS_URL']}/api/catalog",
        warehouse="lake",
        credential=creds["credential"],
        scope="PRINCIPAL_ROLE:ALL",
        **{
            "s3.endpoint": os.environ["S3_ENDPOINT"],
            "s3.access-key-id": os.environ["READER_S3_ACCESS_KEY"],
            "s3.secret-access-key": os.environ["READER_S3_SECRET_KEY"],
            "s3.region": "us-east-1",
            # PyIceberg asks for vended credentials by default; the catalog has none (stsUnavailable), and clients
            # bring their own S3 keys, like Trino (vended-credentials-enabled=false) and the sink.
            "header.X-Iceberg-Access-Delegation": "",
        },
    )


def rows(cat: RestCatalog, table: str) -> list[dict]:
    return cat.load_table(f"bronze.{table}").scan().to_arrow().to_pylist()


def check(cat: RestCatalog, epoch: int) -> list[str]:
    problems = []
    for table, expected in EXPECTED.items():
        data = rows(cat, table)
        events = {(row["id"], row["_op"]) for row in data}
        if missing := expected - events:
            problems.append(f"bronze.{table}: missing events {sorted(missing)}; have {sorted(events)}")
        for row in data:
            if row["_cdc_epoch"] != epoch:
                problems.append(f"bronze.{table}: _cdc_epoch {row['_cdc_epoch']!r} != {epoch}")
            if absent := [c for c in METADATA_COLUMNS if row.get(c) is None]:
                problems.append(f"bronze.{table} id={row['id']}: null metadata columns {absent}")
            if row["_op"] != "d" and table == "orders" and row.get("total") is None:
                problems.append(f"bronze.orders id={row['id']}: decimal column total not converted")
    if not rows(cat, "heartbeat"):
        problems.append("bronze.heartbeat: no rows (heartbeat.action.query did not produce events)")
    return problems


def main() -> int:
    cat = catalog()
    epoch = int(os.environ["CDC_EPOCH"])
    deadline = time.monotonic() + float(sys.argv[1])
    while True:
        problems = check(cat, epoch)
        if not problems or time.monotonic() >= deadline:
            break
        time.sleep(5)
    for table in (*EXPECTED, "heartbeat"):
        data = rows(cat, table)
        snapshots = len(cat.load_table(f"bronze.{table}").metadata.snapshots)
        print(f"bronze.{table}: {len(data)} rows, ops={sorted({r['_op'] for r in data})}, snapshots={snapshots}")
    orders = sorted(rows(cat, "orders"), key=lambda r: (r["id"], r["_lsn"]))
    print("bronze.orders sample:", [(r["id"], r["_op"], r["status"], str(r["total"])) for r in orders])

    try:
        cat.create_namespace("smoke_should_fail")
        problems.append("read-only principal could create a namespace")
    except ForbiddenError:
        print("read-only principal: create_namespace rejected (403), as expected")

    for problem in problems:
        print(f"FAIL {problem}")
    print("PASS" if not problems else "FAILED")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
