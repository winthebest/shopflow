"""lab.trino_client without a stack: airlift durations and data sizes as Trino's REST API prints them."""

import pytest

from lab.trino_client import parse_airlift


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1.23s", 1.23),
        ("450.00ms", 0.45),
        ("2.50m", 150.0),
        ("12.00us", 12e-6),
        ("1.00h", 3600.0),
        ("0.00ns", 0.0),
        ("1024B", 1024.0),
        ("12.50MB", 12.5 * 2**20),
        ("1.00GB", 2.0**30),
        ("3.00kB", 3072.0),
    ],
)
def test_airlift_values_parse(text: str, value: float):
    assert parse_airlift(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["1.2 s", "12", "1.0Mb", "", "-1s"])
def test_other_text_is_refused(text: str):
    with pytest.raises(ValueError):
        parse_airlift(text)
