"""Create bronze tables in the smoke test's Polaris from the real DDL (deploy/platform/trino/base/bronze-tables.sql).

On the cluster Trino runs that file; here there is no Trino, so the CREATE TABLE statements are translated to Iceberg
REST create-table requests (same columns, types, partitioning and format version 2). Standard library only.
Usage: create-bronze-tables.py [<table> ...]   (no argument: every table in the DDL)
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DDL = "/smoke-deploy/bronze-tables.sql"
POLARIS = os.environ["POLARIS_URL"]
TRINO_TO_ICEBERG = {
    "bigint": "long",
    "integer": "int",
    "varchar": "string",
    "timestamp(6) with time zone": "timestamptz",
}
TABLE_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS lake\.bronze\.(\w+) \((.*?)\)\s*WITH \(format_version = (\d), "
    r"partitioning = ARRAY\['day\((\w+)\)'\]\);",
    re.S,
)


def iceberg_type(trino_type: str) -> str:
    if trino_type.startswith("decimal("):
        return trino_type
    return TRINO_TO_ICEBERG[trino_type]


def parse_ddl(text: str) -> dict[str, dict]:
    tables = {}
    for name, body, format_version, partition_column in TABLE_RE.findall(text):
        columns = [line.strip().rstrip(",").split(None, 1) for line in body.strip().splitlines()]
        fields = [
            {"id": i, "name": col, "required": False, "type": iceberg_type(typ.strip())}
            for i, (col, typ) in enumerate(columns, start=1)
        ]
        source_id = next(f["id"] for f in fields if f["name"] == partition_column)
        tables[name] = {
            "name": name,
            "schema": {"type": "struct", "schema-id": 0, "fields": fields},
            "partition-spec": {
                "spec-id": 0,
                "fields": [
                    {"source-id": source_id, "field-id": 1000, "name": f"{partition_column}_day", "transform": "day"}
                ],
            },
            "properties": {"format-version": format_version},
        }
    return tables


def request(method: str, path: str, token: str | None, body: dict | None = None, form: bool = False) -> dict:
    data, headers = None, {}
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{POLARIS}{path}", data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 (fixed http URL from compose)
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        sys.exit(f"{method} {path}: HTTP {error.code} {error.read().decode(errors='replace')}")


def main() -> None:
    with open(DDL) as f:
        tables = parse_ddl(f.read())
    token = request(
        "POST",
        "/api/catalog/v1/oauth/tokens",
        None,
        {
            "grant_type": "client_credentials",
            "client_id": os.environ["POLARIS_ROOT_CLIENT_ID"],
            "client_secret": os.environ["POLARIS_ROOT_CLIENT_SECRET"],
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        form=True,
    )["access_token"]
    for name in sys.argv[1:] or sorted(tables):
        request("POST", "/api/catalog/v1/lake/namespaces/bronze/tables", token, tables[name])
        print(f"created bronze.{name} ({len(tables[name]['schema']['fields'])} columns, format-version 2)")


if __name__ == "__main__":
    main()
