from datetime import UTC, datetime

import pytest

from aws_world import LEASE_PARAM
from reaper.lease import parse_lease, read_lease

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def put_lease(world, value: str) -> None:
    world.client("ssm").put_parameter(Name=LEASE_PARAM, Value=value, Type="String", Overwrite=True)


def test_missing_lease_counts_as_expired(world):
    status = read_lease(world.client("ssm"), LEASE_PARAM, NOW)
    assert status.expired and status.reason == "no lease"


def test_future_lease_is_valid(world):
    put_lease(world, "2026-10-09T13:00:00Z")
    status = read_lease(world.client("ssm"), LEASE_PARAM, NOW)
    assert not status.expired
    assert status.as_dict()["expiresAt"] == "2026-10-09T13:00:00Z"


def test_past_lease_is_expired(world):
    put_lease(world, "2026-10-09T11:59:59Z")
    assert read_lease(world.client("ssm"), LEASE_PARAM, NOW).expired


def test_unreadable_lease_counts_as_expired(world):
    put_lease(world, "tomorrow")
    status = read_lease(world.client("ssm"), LEASE_PARAM, NOW)
    assert status.expired and status.reason == "unreadable lease"


def test_offsets_are_normalised_to_utc():
    assert parse_lease("2026-10-09T19:00:00+07:00") == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError):
        parse_lease("2026-10-09T12:00:00")
