"""lab/stack.sh py: a PyIceberg writer in a container on the lab network commits through Polaris, Trino reads it."""

import subprocess
import uuid

import pytest

from lab.guard import guarded_connect
from lab.trino_client import REPO, Settings, query

pytestmark = pytest.mark.lab

WRITER = """
import pyarrow as pa
from lab.iceberg import rest_catalog
catalog = rest_catalog()
catalog.create_namespace_if_not_exists("py_probe")
schema = pa.schema([("id", pa.int64())])
table = catalog.create_table("py_probe.{name}", schema=schema, properties={{"format-version": "2"}})
table.append(pa.table({{"id": [1, 2, 3]}}))
print("appended", len(table.metadata.snapshots))
"""


def test_a_direct_writer_commits_through_the_zone_catalog(lab: Settings):
    name = f"t_{uuid.uuid4().hex[:10]}"
    out = subprocess.run(
        [str(REPO / "lab/stack.sh"), "py", "exp", "--", "python", "-c", WRITER.format(name=name)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("appended 1")
    conn = guarded_connect("harness", settings=lab)
    try:
        assert query(conn, f"SELECT count(*) FROM lab_exp.py_probe.{name}") == [[3]]
    finally:
        query(conn, f"DROP TABLE lab_exp.py_probe.{name}")
