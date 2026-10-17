"""Fixture `lab`: tests marked `lab` need the lab stack (lab/stack.sh up) and are skipped when it is not running."""

import pytest
from lab_trino import RUN, URL, connect, query


@pytest.fixture(scope="session")
def lab() -> None:
    """Skips unless the stack answers as `harness`; the lab stack is local only (no CI job starts it)."""
    if not (RUN / "passwords/harness").is_file():
        pytest.skip(f"lab stack not set up ({RUN} has no credentials): lab/stack.sh up")
    try:
        query(connect("harness"), "SELECT 1")
    except Exception as error:
        pytest.skip(f"lab Trino not reachable at {URL.geturl()}: {error}")
