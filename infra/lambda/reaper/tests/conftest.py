"""Fixtures for the reaper: every AWS call goes to moto's in-memory backend, never to AWS."""

import boto3
import pytest
from moto import mock_aws

from aws_world import REGION, World


@pytest.fixture(autouse=True)
def _offline_aws(monkeypatch):
    """Fake credentials so nothing can reach a real account, even by accident."""
    for key in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")


@pytest.fixture
def world():
    with mock_aws():
        session = boto3.session.Session(region_name=REGION)
        ec2 = session.client("ec2")
        vpc_id = ec2.create_vpc(CidrBlock="10.60.0.0/16")["Vpc"]["VpcId"]
        subnets = [
            ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=f"{REGION}{az}")["Subnet"]["SubnetId"]
            for cidr, az in (("10.60.0.0/19", "a"), ("10.60.32.0/19", "b"))
        ]
        yield World(session=session, vpc_id=vpc_id, subnet_ids=subnets)
