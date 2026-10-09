"""SIGTERM contract: stop accepting new connections at once, finish in-flight requests, then exit."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

LATENCY_MS = 1500


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_sigterm_drains_in_flight_request():
    port = free_port()
    script = (
        "from payments.main import Settings, create_app; from shopflow_common.server import serve; "
        f"serve(create_app(Settings(payment_latency_ms={LATENCY_MS}, payment_failure_rate=0)), port={port})"
    )
    env = {**os.environ, "OTEL_SDK_DISABLED": "true"}
    proc = subprocess.Popen([sys.executable, "-c", script], env=env)  # noqa: S603 - fixed interpreter and script
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.1)

        result: dict[str, httpx.Response] = {}
        in_flight = threading.Thread(
            target=lambda: result.update(
                response=httpx.post(f"{base}/charges", json={"order_id": 1, "amount": "5.00"}, timeout=10)
            )
        )
        in_flight.start()
        time.sleep(0.3)  # request is now sleeping inside the handler
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)

        with pytest.raises(httpx.ConnectError):
            httpx.get(f"{base}/healthz", timeout=0.5)

        in_flight.join(timeout=10)
        assert result["response"].status_code == 201
        # After draining, uvicorn re-raises the captured SIGTERM, so the process ends "killed by SIGTERM" (143).
        assert proc.wait(timeout=10) in (0, -signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()
