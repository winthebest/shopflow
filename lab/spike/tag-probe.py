"""Tag spike: does an Iceberg tag keep its snapshot from Trino's expire_snapshots, and does a tag with
max_ref_age_ms expire? Runs inside the lab's `setup` container (lab/spike/run.sh) as the research harness: Trino
catalog lab_exp (retention floors 0s) for the table and the procedures, PyIceberg 0.12 with the lab_exp Polaris
principal for the tags (Trino 483 reads refs but cannot create them).

Table: CREATE + 4 INSERTs = snapshots s0 (empty) .. s4 (main). After 40 s every snapshot is older than 30 s; then
  keep_forever   -> s1, no max_ref_age_ms
  keep_1h        -> s2, max_ref_age_ms = 3 600 000
  keep_30s       -> s3, max_ref_age_ms = 30 000   (tag created 40 s after its snapshot, a second before expiring)
  main_30s       -> s4, max_ref_age_ms = 30 000   (the snapshot main points to)
then expire_snapshots(retention_threshold => '0s') and remove_orphan_files(retention_threshold => '0s'), and read
$refs, $snapshots and each tag back (FOR VERSION AS OF). Prints one JSON document; exit code 0 = ran, not "passed".
"""

import json
import os
import time

from pyiceberg.catalog.rest import RestCatalog

import trino

RUN = "/run/lab"


def env_file(path: str) -> dict[str, str]:
    with open(path) as f:
        return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)


def main() -> None:
    with open(f"{RUN}/passwords/harness") as f:
        password = f.read()
    conn = trino.dbapi.connect(
        host="trino",
        port=8443,
        http_scheme="https",
        user="harness",
        auth=trino.auth.BasicAuthentication("harness", password),
        verify=f"{RUN}/trino/tls/cert.pem",
    )

    def q(sql: str) -> list:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()

    name = f"t_{int(time.time())}"
    fq = f"lab_exp.tagspike.{name}"
    q("CREATE SCHEMA IF NOT EXISTS lab_exp.tagspike")
    q(f"CREATE TABLE {fq} (id bigint) WITH (format_version = 2)")
    for i in (1, 2, 3, 4):
        q(f"INSERT INTO {fq} VALUES ({i})")
    snaps = [row[0] for row in q(f'SELECT snapshot_id FROM lab_exp.tagspike."{name}$snapshots" ORDER BY committed_at')]
    time.sleep(40)

    catalog = RestCatalog(
        "lab_exp",
        uri=f"{os.environ['POLARIS_URL']}/api/catalog",
        warehouse="lab_exp",
        credential=env_file(f"{RUN}/polaris.env")["POLARIS_LAB_EXP_CREDENTIAL"],
        scope="PRINCIPAL_ROLE:ALL",
        **{"header.X-Iceberg-Access-Delegation": ""},  # no vended credentials (stsUnavailable)
    )
    table = catalog.load_table(f"tagspike.{name}")
    tags = {
        "keep_forever": (snaps[1], None),
        "keep_1h": (snaps[2], 3_600_000),
        "keep_30s": (snaps[3], 30_000),
        "main_30s": (snaps[4], 30_000),
    }
    with table.manage_snapshots() as ms:
        for tag, (snapshot_id, max_age) in tags.items():
            ms.create_tag(snapshot_id=snapshot_id, tag_name=tag, max_ref_age_ms=max_age)
    refs = f'SELECT name, type, snapshot_id, max_reference_age_in_ms FROM lab_exp.tagspike."{name}$refs" ORDER BY name'
    before = q(refs)
    committed = dict(q(f'SELECT snapshot_id, to_unixtime(committed_at) FROM lab_exp.tagspike."{name}$snapshots"'))
    age_at_expire = {tag: round(time.time() - committed[sid], 1) for tag, (sid, _) in tags.items()}

    q(f"ALTER TABLE {fq} EXECUTE expire_snapshots(retention_threshold => '0s')")
    q(f"ALTER TABLE {fq} EXECUTE remove_orphan_files(retention_threshold => '0s')")

    after = q(refs)
    remaining = {row[0] for row in q(f'SELECT snapshot_id FROM lab_exp.tagspike."{name}$snapshots"')}
    reads = {}
    for tag in tags:
        try:
            reads[tag] = q(f"SELECT count(*) FROM {fq} FOR VERSION AS OF '{tag}'")[0][0]
        except trino.exceptions.TrinoQueryError as e:
            reads[tag] = f"{e.error_name}: {e.message}".splitlines()[0][:160]
    print(
        json.dumps(
            {
                "table": fq,
                "snapshots_in_commit_order": snaps,
                "snapshot_age_seconds_when_expiring": age_at_expire,
                "refs_before": before,
                "refs_after": after,
                "snapshot_kept": {f"s{i}": sid in remaining for i, sid in enumerate(snaps)},
                "read_by_tag_after": reads,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
