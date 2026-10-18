"""PyIceberg's REST catalog for one lab zone, for writers that commit through Polaris directly (as the platform's sinks
do), inside `lab/stack.sh py <zone> -- …`.

The container's environment holds the zone's credentials (stack.sh passes them by name, never as values): the REST
URI, warehouse and principal (`LAB_REST_*`), the S3 endpoint and key (`LAB_S3_*`). rest_catalog() builds the catalog
from them, so experiment code never handles the values. The guard's rules apply: only this stack's Polaris and
SeaweedFS, never warehouse `lake`.
"""

import os

from pyiceberg.catalog.rest import RestCatalog

from lab.guard import LAB_POLARIS_URI, LAB_S3_ENDPOINT, PLATFORM, LakeGuardError

REQUIRED = ("LAB_REST_URI", "LAB_REST_WAREHOUSE", "LAB_REST_CREDENTIAL", "LAB_S3_ENDPOINT", "LAB_S3_ACCESS_KEY",
            "LAB_S3_SECRET_KEY")  # fmt: skip


def catalog_properties(env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ) if env is None else env
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise LakeGuardError(f"no zone credentials ({', '.join(missing)}): run inside lab/stack.sh py <zone> -- …")
    if env["LAB_REST_URI"] != LAB_POLARIS_URI or env["LAB_S3_ENDPOINT"] != LAB_S3_ENDPOINT:
        raise LakeGuardError(f"REST catalog or S3 is not this stack's ({LAB_POLARIS_URI}, {LAB_S3_ENDPOINT})")
    if env["LAB_REST_WAREHOUSE"] == PLATFORM or not env["LAB_REST_WAREHOUSE"].startswith("lab_"):
        raise LakeGuardError(f"warehouse {env['LAB_REST_WAREHOUSE']}: lab warehouses only")
    return {
        "uri": env["LAB_REST_URI"],
        "warehouse": env["LAB_REST_WAREHOUSE"],
        "credential": env["LAB_REST_CREDENTIAL"],
        "scope": "PRINCIPAL_ROLE:ALL",
        "header.X-Iceberg-Access-Delegation": "",  # the lab's Polaris vends no credentials (stsUnavailable)
        "s3.endpoint": env["LAB_S3_ENDPOINT"],
        "s3.access-key-id": env["LAB_S3_ACCESS_KEY"],
        "s3.secret-access-key": env["LAB_S3_SECRET_KEY"],
        "s3.path-style-access": "true",
        "s3.region": "us-east-1",
    }


def rest_catalog(name: str = "lab") -> RestCatalog:
    return RestCatalog(name, **catalog_properties())
