"""Fixture `lab`: the lab stack (lab/stack.sh up) for tests marked `lab`; skips without it, fails if LAB_REQUIRED=1."""

import os

import pytest
from lab_trino import RUN, URL, connect, query


@pytest.fixture(scope="session")
def lab() -> None:
    """Stops unless the stack answers as `harness`; the lab stack is local only (no CI job starts it)."""
    stop = pytest.fail if os.environ.get("LAB_REQUIRED") == "1" else pytest.skip
    if not (RUN / "passwords/harness").is_file():
        stop(f"lab stack not set up ({RUN} has no credentials): lab/stack.sh up")
    try:
        query(connect("harness"), "SELECT 1")
    except Exception as error:
        stop(f"lab Trino not reachable at {URL.geturl()}: {error}")
