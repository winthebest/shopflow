"""Idempotent Polaris setup for the lab: catalogs (one warehouse per zone), namespaces, principals and their grants.

Usage: python -I polaris-setup.py SPEC.json [SPEC.json ...]   (lab/stack.sh runs it in the compose `setup` service)

Each spec file holds {"catalogs": [{"name", "location", "properties"?, "namespaces"?}],
"principals": [{"name", "catalog", "grants": [{"on": {"type": "catalog"} | {"type": "namespace", "namespace": [...]},
"privileges": [...]}]}]}.
Every principal gets a principal role and a catalog role of its own name, so its grants never mix with another's.
Catalog properties apply when the catalog is created (an existing catalog is left as it is): change them by a reset.

Polaris generates the client ids, so credentials cannot be pre-generated. They go to OUTPUT_ENV_FILE as
POLARIS_<PRINCIPAL>_CREDENTIAL=<id>:<secret> (Trino's oauth2.credential format), read by the trino service through
env_file. A principal that already exists keeps its credential when the file has it, and is rotated otherwise.
Same approach as the platform's deploy/platform/iceberg-catalog/base/polaris-setup.py; standard library only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLARIS = os.environ["POLARIS_URL"].rstrip("/")
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
OUTPUT = os.environ["OUTPUT_ENV_FILE"]


def call(method: str, path: str, body=None, token=None, form=False) -> tuple[int, dict]:
    url = f"{POLARIS}{path}"
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-HTTP URL: {url}")
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 (scheme checked)
    for attempt in range(30):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (scheme checked)
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw) if raw else {}
            except ValueError:
                return error.code, {"raw": raw.decode(errors="replace")}
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            # Every write is idempotent (409 counts as success), so repeating a request whose reply was lost is safe.
            print(f"{method} {path}: {error}; retry {attempt + 1}", file=sys.stderr)
            time.sleep(2)
    sys.exit(f"{method} {path}: gave up")


def expect(result: tuple[int, dict], ok: tuple[int, ...], what: str) -> dict:
    status, payload = result
    if status not in ok:
        sys.exit(f"{what}: HTTP {status} {payload}")
    return payload


def read_env_file() -> dict[str, str]:
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT) as f:
        return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)


def write_env_file(values: dict[str, str]) -> None:
    tmp = f"{OUTPUT}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(f"{k}={v}\n" for k, v in sorted(values.items()))
    os.replace(tmp, OUTPUT)


def main() -> None:
    specs = []
    for path in sys.argv[1:]:
        with open(path) as f:
            specs.append(json.load(f))
    token = expect(
        call(
            "POST",
            "/api/catalog/v1/oauth/tokens",
            {
                "grant_type": "client_credentials",
                "client_id": "root",
                "client_secret": os.environ["POLARIS_ROOT_SECRET"],
                "scope": "PRINCIPAL_ROLE:ALL",
            },
            form=True,
        ),
        (200,),
        "root token",
    )["access_token"]
    mgmt = "/api/management/v1"

    for catalog in (c for spec in specs for c in spec.get("catalogs", [])):
        name, location = catalog["name"], catalog["location"]
        body = {
            "catalog": {
                "name": name,
                "type": "INTERNAL",
                "properties": {"default-base-location": location, **catalog.get("properties", {})},
                "storageConfigInfo": {
                    "storageType": "S3",
                    "allowedLocations": [location],
                    "endpoint": S3_ENDPOINT,
                    "pathStyleAccess": True,
                    "stsUnavailable": True,
                    "region": "us-east-1",
                },
            }
        }
        expect(call("POST", f"{mgmt}/catalogs", body, token), (201, 409), f"catalog {name}")
        for namespace in catalog.get("namespaces", []):
            result = call("POST", f"/api/catalog/v1/{name}/namespaces", {"namespace": namespace}, token)
            expect(result, (200, 409), f"namespace {name}.{'.'.join(namespace)}")
        print(f"catalog {name}: {location}")

    credentials = read_env_file()
    for principal in (p for spec in specs for p in spec.get("principals", [])):
        name, catalog = principal["name"], principal["catalog"]
        role = name
        expect(
            call("POST", f"{mgmt}/catalogs/{catalog}/catalog-roles", {"catalogRole": {"name": role}}, token),
            (201, 409),
            f"catalog role {catalog}/{role}",
        )
        for grant in principal["grants"]:
            for privilege in grant["privileges"]:
                body = {"grant": {**grant["on"], "privilege": privilege}}
                expect(
                    call("PUT", f"{mgmt}/catalogs/{catalog}/catalog-roles/{role}/grants", body, token),
                    (200, 201),
                    f"grant {privilege} on {grant['on']} to {role}",
                )
        expect(
            call("POST", f"{mgmt}/principal-roles", {"principalRole": {"name": role}}, token),
            (201, 409),
            f"principal role {role}",
        )
        expect(
            call(
                "PUT", f"{mgmt}/principal-roles/{role}/catalog-roles/{catalog}", {"catalogRole": {"name": role}}, token
            ),
            (200, 201),
            f"assign catalog role {catalog}/{role}",
        )

        key = f"POLARIS_{name.upper()}_CREDENTIAL"
        status, payload = call("POST", f"{mgmt}/principals", {"principal": {"name": name}}, token)
        if status == 409 and key in credentials:
            print(f"principal {name}: exists, credential kept")
        else:
            if status == 409:
                rotated = call("POST", f"{mgmt}/principals/{name}/rotate", None, token)
                payload = expect(rotated, (200,), f"rotate {name}")
            else:
                expect((status, payload), (200, 201), f"principal {name}")
            creds = payload["credentials"]
            credentials[key] = f"{creds['clientId']}:{creds['clientSecret']}"
            print(f"principal {name}: credential written")
        expect(
            call("PUT", f"{mgmt}/principals/{name}/principal-roles", {"principalRole": {"name": role}}, token),
            (200, 201),
            f"assign principal role {role} to {name}",
        )

    write_env_file(credentials)
    print("polaris setup complete")


if __name__ == "__main__":
    main()
