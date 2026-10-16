# /// script
# requires-python = ">=3.12"
# dependencies = ["trino==0.340.0"]
# ///
"""Boundary spike: which layer stops which operation, measured on the lab stack (lab/stack.sh up spike).

Every spike_* catalog points at the same Polaris warehouse `spike`, and each one turns on exactly one restriction
(lab/spike/trino/catalog/*.properties say which). For each catalog and operation, a pristine table (created through
spike_open: CREATE + 3 INSERTs = 4 snapshots, 3 data files, plus one planted orphan file) gets the operation through
that catalog as identity `spike`; the result is the error Trino returned, if any, and the table's state afterwards,
read back through spike_open. "denied" = an error; "acted" = no error and the state changed as the operation should;
"no effect" = no error, state unchanged. Then the OPA decision log shows what Trino asked OPA.

Usage: uv run lab/spike/boundary-probe.py [--json out/lab/spike/boundary.json]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import trino

REPO = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get("LAB_RUN_DIR", REPO / "out/lab"))
COMPOSE = [
    "docker",
    "compose",
    "--project-directory",
    str(REPO / "lab"),
    "--env-file",
    str(RUN / ".env"),
    "-f",
    str(REPO / "lab/compose.yml"),
    "-f",
    str(REPO / "lab/spike/compose.yml"),
]

# (catalog, namespace): the layer each probe catalog isolates.
TARGETS = [
    ("spike_open", "probe", "none (control)"),
    ("spike_engine_ro", "probe", "Trino connector: iceberg.security=READ_ONLY"),
    ("spike_acl_ro", "probe", "Trino file ACL: catalog allow read-only"),
    ("spike_acl_noprivs", "probe", "Trino file ACL: no table privileges, not schema owner"),
    ("spike_rest_ro", "probe", "Polaris: catalog-wide read privileges"),
    ("spike_rest_ns", "granted", "Polaris: namespace grant (write) on this namespace"),
    ("spike_rest_ns", "ungranted", "Polaris: namespace grant (read only) on this namespace"),
    ("spike_s3_ro", "probe", "S3: SeaweedFS key with Read + List only"),
    ("spike_opa", "probe", "OPA: policy denies procedures on this catalog"),
    ("spike_hidden", "probe", "Trino file ACL: catalog allow none"),
]

# name -> (SQL template, check of the state afterwards: True = the operation took effect)
OPERATIONS = {
    "SELECT": ("SELECT count(*) FROM {t}", None),
    "SET SESSION expire floor": ("SET SESSION {c}.expire_snapshots_min_retention = '0s'", None),
    "SET SESSION orphan floor": ("SET SESSION {c}.remove_orphan_files_min_retention = '0s'", None),
    "INSERT": ("INSERT INTO {t} VALUES (99, 'z')", lambda s: s["rows"] == 4),
    "DELETE": ("DELETE FROM {t} WHERE id = 1", lambda s: s["rows"] == 2),
    "SET PROPERTIES": ("ALTER TABLE {t} SET PROPERTIES format = 'ORC'", lambda s: s["format"] == "ORC"),
    "optimize": ("ALTER TABLE {t} EXECUTE optimize", lambda s: s["files"] == 1),
    "expire_snapshots": (
        "ALTER TABLE {t} EXECUTE expire_snapshots(retention_threshold => '0s')",
        lambda s: s["snapshots"] == 1,
    ),
    "remove_orphan_files": (
        "ALTER TABLE {t} EXECUTE remove_orphan_files(retention_threshold => '0s')",
        lambda s: not s["orphan"],
    ),
    "rollback_to_snapshot": (
        "ALTER TABLE {t} EXECUTE rollback_to_snapshot(snapshot_id => {first_insert})",
        lambda s: s["rows"] == 1,
    ),
    "CALL unregister_table": (
        "CALL {c}.system.unregister_table(schema_name => '{n}', table_name => '{name}')",
        lambda s: not s["exists"],
    ),
    "DROP TABLE": ("DROP TABLE {t}", lambda s: not s["exists"]),
    "CREATE TABLE": ("CREATE TABLE {c}.{n}.{name}_new (id bigint) WITH (format_version = 2)", None),
}


def connect() -> trino.dbapi.Connection:
    user = "spike"
    return trino.dbapi.connect(
        host="127.0.0.1",
        port=28443,
        http_scheme="https",
        user=user,
        auth=trino.auth.BasicAuthentication(user, (RUN / "passwords" / user).read_text()),
        verify=str(RUN / "trino/tls/cert.pem"),
    )


def run(conn: trino.dbapi.Connection, sql: str) -> list:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def filer(*args: str) -> str:
    command = [*COMPOSE, "exec", "-T", "seaweedfs", "curl", "-sS", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


class Table:
    """A probe table, created and inspected through spike_open (no restriction)."""

    counter = 0

    def __init__(self, admin: trino.dbapi.Connection, namespace: str, label: str) -> None:
        Table.counter += 1
        self.admin, self.namespace = admin, namespace
        self.name = f"{label}_{int(time.time())}_{Table.counter}"
        self.fq = f"spike_open.{namespace}.{self.name}"
        run(admin, f"CREATE TABLE {self.fq} (id bigint, v varchar) WITH (format_version = 2)")
        for i in (1, 2, 3):
            run(admin, f"INSERT INTO {self.fq} VALUES ({i}, 'v{i}')")
        snapshots = run(
            admin, f'SELECT snapshot_id FROM spike_open.{namespace}."{self.name}$snapshots" ORDER BY committed_at'
        )
        self.first_insert = snapshots[1][0]  # snapshots[0] is CREATE TABLE's empty snapshot
        data_file = run(admin, f'SELECT file_path FROM spike_open.{namespace}."{self.name}$files" LIMIT 1')[0][0]
        self.data_dir = "/buckets/" + data_file.removeprefix("s3://").rsplit("/", 1)[0]
        status = filer(
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-X",
            "PUT",
            "--data-binary",
            "orphan",
            f"http://127.0.0.1:8888{self.data_dir}/orphan-planted.parquet",
        )
        if status != "201":
            sys.exit(f"planting the orphan file failed: HTTP {status}")
        self.pristine = self.state()

    def state(self) -> dict:
        exists = bool(
            run(
                self.admin,
                "SELECT 1 FROM spike_open.information_schema.tables "
                f"WHERE table_schema = '{self.namespace}' AND table_name = '{self.name}'",
            )
        )
        if not exists:
            return {"exists": False, "rows": None, "snapshots": None, "files": None, "format": None, "orphan": None}
        q = f'spike_open.{self.namespace}."{self.name}'
        listing = filer("-H", "Accept: application/json", f"http://127.0.0.1:8888{self.data_dir}/")
        return {
            "exists": True,
            "rows": run(self.admin, f"SELECT count(*) FROM {self.fq}")[0][0],
            "snapshots": run(self.admin, f'SELECT count(*) FROM {q}$snapshots"')[0][0],
            "files": run(self.admin, f'SELECT count(*) FROM {q}$files"')[0][0],
            "format": run(self.admin, f"SELECT value FROM {q}$properties\" WHERE key = 'write.format.default'")[0][0],
            "orphan": "orphan-planted.parquet" in listing,
        }


def probe(admin, conn, catalog: str, namespace: str, label: str) -> list[dict]:
    results, table = [], None
    for op, (template, took_effect) in OPERATIONS.items():
        if table is None or table.state() != table.pristine:
            table = Table(admin, namespace, label)
        sql = template.format(
            t=f"{catalog}.{namespace}.{table.name}",
            c=catalog,
            n=namespace,
            name=table.name,
            first_insert=table.first_insert,
        )
        try:
            run(conn, sql)
            error = None
        except trino.exceptions.TrinoQueryError as e:
            error = f"{e.error_name}: {e.message}".splitlines()[0][:220]
        after = table.state()
        if error:
            outcome = "denied"
        elif took_effect is None:
            outcome = "allowed"
        else:
            outcome = "acted" if took_effect(after) else "no effect"
        results.append(
            {
                "catalog": catalog,
                "namespace": namespace,
                "operation": op,
                "outcome": outcome,
                "error": error,
                "state_after": after,
                "sql": sql,
            }
        )
        print(f"{catalog:18} {namespace:9} {op:25} {outcome:9} {error or ''}", flush=True)
    return results


def opa_decisions(since: str) -> list[dict]:
    logs = subprocess.run(
        [*COMPOSE, "logs", "--no-log-prefix", "--since", since, "opa"], check=True, capture_output=True, text=True
    ).stdout
    decisions = []
    for line in logs.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        action = (entry.get("input") or {}).get("action") or {}
        if action.get("operation") in ("ExecuteTableProcedure", "ExecuteProcedure"):
            decisions.append(
                {
                    "operation": action["operation"],
                    "resource": action.get("resource"),
                    "user": entry["input"]["context"]["identity"]["user"],
                    "result": entry.get("result"),
                }
            )
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=RUN / "spike/boundary.json")
    args = parser.parse_args()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    admin, conn = connect(), connect()
    results = []
    for catalog, namespace, layer in TARGETS:
        label = f"{catalog.removeprefix('spike_')}_{namespace}"
        for row in probe(admin, conn, catalog, namespace, label):
            results.append({**row, "layer": layer})
        conn = connect()  # a fresh session: SET SESSION must not carry over to the next catalog
    decisions = opa_decisions(since)
    print(f"\nOPA saw {len(decisions)} procedure checks:")
    for d in decisions:
        print(json.dumps(d, sort_keys=True))
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({"started": since, "results": results, "opa": decisions}, indent=1, default=str))
    print(f"\nwritten: {args.json}")


if __name__ == "__main__":
    main()
