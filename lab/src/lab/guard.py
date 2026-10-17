"""The harness refuses the platform's lakehouse, and records results only on the lab stack.

The platform's Trino (deploy/platform/trino) serves catalogs `lake` and `lake_ro` on Polaris warehouse `lake`, bucket
`lake`. The lab has its own Polaris, SeaweedFS and warehouses on buckets `lab-*` (lab/polaris.json). Before a session
is handed out, and for every table the harness touches:

- the lab's catalog files ($LAB_RUN_DIR/trino/catalog, rendered by lab/stack.sh): every Iceberg catalog uses this
  stack's Polaris and SeaweedFS, and none is named `lake*` or uses warehouse `lake`;
- the Polaris specs (lab/polaris.json and overlays): no warehouse `lake`, every location under s3://lab-*/;
- the server: an identity that sees a `lake*` catalog is on the platform's Trino. Results (dev, baseline, held-out,
  eval) also need the lab's node environment, `lab` (lab/trino/etc/node.properties): LAB_TRINO_URL may point at k3d
  for exploring (`require_lab=False`), never for numbers;
- targets: the harness writes only to lab catalogs (`lab_exp`, `lab_ops`, `lab_ro`, `lab_sbx`, `lab_base_<run>`), and
  every table it touches is stored under s3://lab-*/.
"""

import json
import re
from collections.abc import Iterable
from pathlib import Path

import trino

from lab.trino_client import Settings, connect, query, server_info

LAB_ENVIRONMENT = "lab"
LAB_POLARIS_URI = "http://polaris:8181/api/catalog"
LAB_S3_ENDPOINT = "http://seaweedfs:8333"
PLATFORM = "lake"

LAB_CATALOG = re.compile(r"lab_(exp|ops|ro|sbx|base_[a-z0-9_]+)")
LAB_LOCATION = re.compile(r"s3://lab-[a-z0-9-]+/.*")
_SHOWN_LOCATION = re.compile(r"\blocation = '([^']*)'")


class LakeGuardError(RuntimeError):
    """The harness refuses to run: the target is, or may be, the platform's lakehouse, or results need the lab."""


def _platform_name(name: str) -> bool:
    return name == PLATFORM or name.startswith(PLATFORM + "_")


def read_properties(path: Path) -> dict[str, str]:
    properties = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "!")) and "=" in line:
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
    return properties


def check_catalog_files(directory: Path) -> list[str]:
    """Names of the Iceberg catalogs in `directory`; refuses any that could reach the platform's lakehouse."""
    names = []
    for path in sorted(directory.glob("*.properties")):
        properties = read_properties(path)
        if properties.get("connector.name") != "iceberg":
            continue
        problems = []
        if _platform_name(path.stem):
            problems.append("its name is the platform's")
        if properties.get("iceberg.rest-catalog.warehouse") == PLATFORM:
            problems.append(f"warehouse {PLATFORM}")
        rest_uri = properties.get("iceberg.rest-catalog.uri")
        if properties.get("iceberg.catalog.type") != "rest" or rest_uri != LAB_POLARIS_URI:
            problems.append(f"catalog is not this stack's Polaris ({LAB_POLARIS_URI})")
        if properties.get("s3.endpoint") != LAB_S3_ENDPOINT:
            problems.append(f"S3 is not this stack's SeaweedFS ({LAB_S3_ENDPOINT})")
        if problems:
            raise LakeGuardError(f"catalog file {path}: {'; '.join(problems)}")
        names.append(path.stem)
    return names


def check_polaris_spec(path: Path) -> list[str]:
    """Names of the Polaris catalogs (warehouses) a spec for lab/polaris-setup.py creates; refuses `lake`."""
    names = []
    for catalog in json.loads(path.read_text()).get("catalogs", []):
        name, location = catalog["name"], catalog["location"]
        if _platform_name(name) or not LAB_LOCATION.fullmatch(location.rstrip("/") + "/"):
            raise LakeGuardError(f"{path}: warehouse {name} at {location}; lab warehouses are lab_* on s3://lab-*/")
        names.append(name)
    return names


def check_visible_catalogs(catalogs: Iterable[str]) -> None:
    platform = sorted(c for c in catalogs if _platform_name(c))
    if platform:
        raise LakeGuardError(f"this identity sees the platform's catalogs {platform}: not the lab's Trino")


def check_environment(environment: str, *, require_lab: bool) -> None:
    if require_lab and environment != LAB_ENVIRONMENT:
        raise LakeGuardError(
            f"Trino's node environment is {environment!r}, not {LAB_ENVIRONMENT!r}: results (dev, baseline, held-out, "
            "eval) come only from the lab stack"
        )


def check_target_catalog(catalog: str) -> None:
    if not LAB_CATALOG.fullmatch(catalog):
        raise LakeGuardError(
            f"catalog {catalog}: the harness works only in lab_exp, lab_ops, lab_ro, lab_sbx, lab_base_<run>"
        )


def check_location(location: str) -> None:
    if not LAB_LOCATION.fullmatch(location):
        raise LakeGuardError(f"location {location}: lab tables are stored under s3://lab-*/")


def shown_location(show_create: str) -> str:
    """The `location` property of SHOW CREATE TABLE / SCHEMA output."""
    match = _SHOWN_LOCATION.search(show_create)
    if not match:
        raise LakeGuardError("SHOW CREATE gave no location: cannot tell where the table is stored")
    return match.group(1)


def guarded_connect(user: str, *, require_lab: bool = True, settings: Settings | None = None) -> trino.dbapi.Connection:
    """A connection as `user`, after the checks above; the default demands the lab stack."""
    settings = settings or Settings.from_env()
    catalog_dir = settings.run_dir / "trino/catalog"
    if catalog_dir.is_dir():
        check_catalog_files(catalog_dir)
    elif require_lab:
        raise LakeGuardError(f"{catalog_dir} does not exist: the lab stack is not set up (lab/stack.sh up)")
    check_environment(server_info(user, settings)["environment"], require_lab=require_lab)
    conn = connect(user, settings)
    check_visible_catalogs(row[0] for row in query(conn, "SHOW CATALOGS"))
    return conn


def check_table(conn: trino.dbapi.Connection, table: str) -> str:
    """Refuses a table outside the lab catalogs or stored outside the lab buckets; returns its location."""
    check_target_catalog(table.split(".", 1)[0])
    location = shown_location(query(conn, f"SHOW CREATE TABLE {table}")[0][0])
    check_location(location)
    return location
