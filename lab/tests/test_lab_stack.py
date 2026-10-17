"""The running lab stack: the guard admits its identities and tables, and the OTel collector writes what it receives."""

import json
import secrets
import socket
import time

import pytest
import requests

from lab.guard import check_table, guarded_connect
from lab.trino_client import Settings, query

pytestmark = pytest.mark.lab

OTLP_HTTP = "http://127.0.0.1:24318"
OTLP_GRPC = ("127.0.0.1", 24317)


@pytest.mark.parametrize("user", ["harness", "agent_planner", "executor"])
def test_the_guard_admits_every_lab_identity(lab: Settings, user: str):
    query(guarded_connect(user, settings=lab), "SELECT 1")


def test_a_table_the_harness_creates_is_stored_in_the_lab_bucket(lab: Settings):
    conn = guarded_connect("harness", settings=lab)
    table = f"lab_exp.guard_probe.t_{int(time.time())}"
    query(conn, "CREATE SCHEMA IF NOT EXISTS lab_exp.guard_probe")
    query(conn, f"CREATE TABLE {table} (id bigint) WITH (format_version = 2)")
    try:
        assert check_table(conn, table).startswith("s3://lab-exp/warehouse/guard_probe/")
    finally:
        query(conn, f"DROP TABLE {table}")
        query(conn, "DROP SCHEMA lab_exp.guard_probe")


def test_spans_sent_over_otlp_http_reach_the_trace_file(lab: Settings):
    trace_id, now = secrets.token_hex(16), time.time_ns()
    span = {
        "traceId": trace_id,
        "spanId": secrets.token_hex(8),
        "name": "otlp-roundtrip",
        "kind": 1,
        "startTimeUnixNano": str(now),
        "endTimeUnixNano": str(now + 1_000_000),
    }
    body = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "lab-tests"}}]},
                "scopeSpans": [{"scope": {"name": "lab.tests"}, "spans": [span]}],
            }
        ]
    }
    requests.post(f"{OTLP_HTTP}/v1/traces", json=body, timeout=10).raise_for_status()
    traces = lab.run_dir / "otel/traces.jsonl"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if traces.is_file() and trace_id in traces.read_text():
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"trace {trace_id} not in {traces} after 20 s")
    line = next(line for line in traces.read_text().splitlines() if trace_id in line)
    spans = [s for rs in json.loads(line)["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]
    assert any(s["traceId"] == trace_id and s["name"] == "otlp-roundtrip" for s in spans)


def test_the_otlp_grpc_port_accepts_connections(lab: Settings):
    with socket.create_connection(OTLP_GRPC, timeout=5):
        pass
