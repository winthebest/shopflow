"""Fixture `lab`: the lab stack (lab/stack.sh up) for tests marked `lab`; skips without it, fails if LAB_REQUIRED=1."""

import os

import pytest

from lab.trino_client import Settings, connect, query


@pytest.fixture(scope="session")
def lab() -> Settings:
    """The stack's settings once Trino answers as `harness`; the lab stack is local only (no CI job starts it)."""
    settings = Settings.from_env()
    stop = pytest.fail if os.environ.get("LAB_REQUIRED") == "1" else pytest.skip
    if not (settings.password_dir / "harness").is_file():
        stop(f"lab stack not set up ({settings.run_dir} has no credentials): lab/stack.sh up")
    try:
        query(connect("harness", settings), "SELECT 1")
    except Exception as error:
        stop(f"lab Trino not reachable at {settings.url.geturl()}: {error}")
    return settings
