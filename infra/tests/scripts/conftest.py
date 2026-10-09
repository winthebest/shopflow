import pytest

from script_harness import Harness


@pytest.fixture
def fake(tmp_path) -> Harness:
    return Harness(tmp_path)
