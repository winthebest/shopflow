"""lab.iceberg without a stack: the zone's environment becomes PyIceberg properties, and nothing else does."""

import pytest

from lab.guard import LakeGuardError
from lab.iceberg import catalog_properties

ENV = {
    "LAB_REST_URI": "http://polaris:8181/api/catalog",
    "LAB_REST_WAREHOUSE": "lab_exp",
    "LAB_REST_CREDENTIAL": "client:secret",
    "LAB_S3_ENDPOINT": "http://seaweedfs:8333",
    "LAB_S3_ACCESS_KEY": "access",
    "LAB_S3_SECRET_KEY": "key",
}


def test_the_zone_environment_becomes_catalog_properties():
    props = catalog_properties(ENV)
    assert (props["warehouse"], props["credential"]) == ("lab_exp", "client:secret")
    assert props["s3.access-key-id"] == "access"
    assert props["s3.path-style-access"] == "true" and props["scope"] == "PRINCIPAL_ROLE:ALL"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LAB_REST_WAREHOUSE", "lake"),
        ("LAB_REST_WAREHOUSE", "spike"),
        ("LAB_REST_URI", "http://polaris.lakehouse.svc:8181/api/catalog"),
        ("LAB_S3_ENDPOINT", "http://seaweedfs.lakehouse.svc:8333"),
        ("LAB_S3_SECRET_KEY", ""),
    ],
)
def test_anything_but_a_lab_zone_is_refused(name: str, value: str):
    with pytest.raises(LakeGuardError):
        catalog_properties({**ENV, name: value})
