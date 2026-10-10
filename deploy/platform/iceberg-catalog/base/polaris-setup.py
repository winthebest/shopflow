"""Idempotent Polaris setup: catalog `lake`, namespace `bronze`, one principal per client, and their grants.

Polaris generates every principal's client id, so the credentials cannot be pre-generated in git. This Job writes
each principal's credentials into a Kubernetes Secret next to its consumer (key `credential` = "<id>:<secret>", the
format of Iceberg's `credential` / Trino's `oauth2.credential`). A principal whose Secret already exists is left
alone; a principal without one gets rotated credentials (for example after a restore into a new cluster).

Standard library only, so it runs on the stock python image. Configuration comes from environment variables.
Outside Kubernetes (the Connect image smoke test), SECRET_OUTPUT_DIR makes it write <dir>/<namespace>/<name>.properties
files instead, readable by Kafka Connect's FileConfigProvider.
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLARIS = os.environ["POLARIS_URL"].rstrip("/")
CATALOG = os.environ.get("CATALOG_NAME", "lake")
BASE_LOCATION = os.environ["CATALOG_BASE_LOCATION"]  # s3://lake/warehouse
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_REGION = os.environ.get("S3_REGION", "us-east-1")

# Connection errors are retried for this long (seconds). Under the k3s NetworkPolicies a new pod's IP reaches the
# server side's allow-list a few seconds after the pod starts; until then kube-router REJECTs its packets, which shows
# up as "Connection refused". A Job retry does not help: its new pod hits the same window
# (docs/runbooks/data-setup-hooks.md).
CONNECT_DEADLINE = float(os.environ.get("CONNECT_DEADLINE_SECONDS", "60"))

READ = ["NAMESPACE_LIST", "NAMESPACE_READ_PROPERTIES", "TABLE_LIST", "TABLE_READ_PROPERTIES", "TABLE_READ_DATA"]
WRITE = ["TABLE_LIST", "TABLE_READ_PROPERTIES", "TABLE_READ_DATA", "TABLE_WRITE_DATA"]

# principal -> where its credentials go, and what it may do. Catalog-scoped grants apply to every namespace.
PRINCIPALS = [
    {  # Kafka Connect Iceberg sink: append to bronze tables, nothing else.
        "name": "iceberg_sink",
        "secret": ("kafka", "polaris-iceberg-sink"),
        "role": "bronze_writer",
        "grants": [{"type": "namespace", "namespace": ["bronze"], "privilege": p} for p in WRITE],
    },
    {  # Trino catalog `lake` (dbt, maintenance): full content management.
        "name": "trino_lake",
        "secret": ("lakehouse", "polaris-trino-lake"),
        "role": "lake_admin",
        "grants": [{"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}],
    },
    {  # Trino catalog `lake_ro` (exporter, metabase, planner): read only, even if Trino's read_only were bypassed.
        "name": "trino_lake_ro",
        "secret": ("lakehouse", "polaris-trino-lake-ro"),
        "role": "lake_reader",
        "grants": [{"type": "catalog", "privilege": p} for p in READ],
    },
]


def call(method: str, url: str, body=None, token=None, form=False, context=None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-HTTP URL: {url}")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 (scheme checked)
    start, delay = time.monotonic(), 1.0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310 (scheme checked)
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw) if raw else {}
            except ValueError:
                return error.code, {"raw": raw.decode(errors="replace")}
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            # Every write here is idempotent (409 counts as success, a missing Secret leads to a rotation), so
            # repeating a request whose reply was lost is safe.
            if time.monotonic() + delay > start + CONNECT_DEADLINE:
                sys.exit(f"{method} {url}: {error} (gave up after {time.monotonic() - start:.0f}s)")
            print(f"{method} {url}: {error}; retrying in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 8.0)


def expect(status: int, payload: dict, ok: tuple[int, ...], what: str) -> dict:
    if status not in ok:
        sys.exit(f"{what}: HTTP {status} {payload}")
    return payload


class Kubernetes:
    """Minimal Secret client using the pod's service account."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self) -> None:
        with open(f"{self.SA}/token") as f:
            self.token = f.read().strip()
        self.context = ssl.create_default_context(cafile=f"{self.SA}/ca.crt")
        self.api = "https://kubernetes.default.svc/api/v1"

    def secret_exists(self, namespace: str, name: str) -> bool:
        status, payload = call(
            "GET", f"{self.api}/namespaces/{namespace}/secrets/{name}", token=self.token, context=self.context
        )
        expect(status, payload, (200, 404), f"get secret {namespace}/{name}")
        return status == 200

    def write_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": "polaris-setup"},
            },
            "type": "Opaque",
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
        }
        url = f"{self.api}/namespaces/{namespace}/secrets"
        status, payload = call("POST", url, body, token=self.token, context=self.context)
        if status == 409:
            status, payload = call("PUT", f"{url}/{name}", body, token=self.token, context=self.context)
        expect(status, payload, (200, 201), f"write secret {namespace}/{name}")


class PropertiesFiles:
    """Same interface as Kubernetes, backed by <dir>/<namespace>/<name>.properties (smoke test only)."""

    def __init__(self, root: str) -> None:
        self.root = root

    def _path(self, namespace: str, name: str) -> str:
        return os.path.join(self.root, namespace, f"{name}.properties")

    def secret_exists(self, namespace: str, name: str) -> bool:
        return os.path.exists(self._path(namespace, name))

    def write_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        os.makedirs(os.path.join(self.root, namespace), exist_ok=True)
        with open(self._path(namespace, name), "w") as f:
            f.writelines(f"{k}={v}\n" for k, v in data.items())


def root_token() -> str:
    _, payload = call(
        "POST",
        f"{POLARIS}/api/catalog/v1/oauth/tokens",
        {
            "grant_type": "client_credentials",
            "client_id": os.environ["POLARIS_ROOT_CLIENT_ID"],
            "client_secret": os.environ["POLARIS_ROOT_CLIENT_SECRET"],
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        form=True,
    )
    if "access_token" not in payload:
        sys.exit(f"root token request failed: {payload}")
    return payload["access_token"]


def ensure(token: str, path: str, body: dict, what: str) -> None:
    """POST a management resource; 409 (already exists) counts as success."""
    status, payload = call("POST", f"{POLARIS}/api/management/v1{path}", body, token)
    expect(status, payload, (200, 201, 204, 409), what)


def put(token: str, path: str, body: dict, what: str) -> None:
    status, payload = call("PUT", f"{POLARIS}/api/management/v1{path}", body, token)
    expect(status, payload, (200, 201, 204), what)


def main() -> None:
    token = root_token()
    kube = PropertiesFiles(os.environ["SECRET_OUTPUT_DIR"]) if os.environ.get("SECRET_OUTPUT_DIR") else Kubernetes()

    ensure(
        token,
        "/catalogs",
        {
            "catalog": {
                "name": CATALOG,
                "type": "INTERNAL",
                "properties": {"default-base-location": BASE_LOCATION},
                "storageConfigInfo": {
                    "storageType": "S3",
                    "allowedLocations": [BASE_LOCATION],
                    "endpoint": S3_ENDPOINT,
                    "pathStyleAccess": True,
                    "stsUnavailable": True,
                    "region": S3_REGION,
                },
            }
        },
        f"create catalog {CATALOG}",
    )

    status, payload = call("POST", f"{POLARIS}/api/catalog/v1/{CATALOG}/namespaces", {"namespace": ["bronze"]}, token)
    expect(status, payload, (200, 409), "create namespace bronze")

    for spec in PRINCIPALS:
        name, role = spec["name"], spec["role"]
        ensure(token, f"/catalogs/{CATALOG}/catalog-roles", {"catalogRole": {"name": role}}, f"catalog role {role}")
        for grant in spec["grants"]:
            put(token, f"/catalogs/{CATALOG}/catalog-roles/{role}/grants", {"grant": grant}, f"grant {grant}")
        ensure(token, "/principal-roles", {"principalRole": {"name": role}}, f"principal role {role}")
        put(
            token,
            f"/principal-roles/{role}/catalog-roles/{CATALOG}",
            {"catalogRole": {"name": role}},
            f"assign catalog role {role}",
        )

        namespace, secret = spec["secret"]
        status, payload = call("POST", f"{POLARIS}/api/management/v1/principals", {"principal": {"name": name}}, token)
        if status == 409:
            if kube.secret_exists(namespace, secret):
                print(f"principal {name}: exists, credentials already in {namespace}/{secret}")
                payload = None
            else:
                status, payload = call("POST", f"{POLARIS}/api/management/v1/principals/{name}/rotate", None, token)
                expect(status, payload, (200,), f"rotate credentials of {name}")
        else:
            expect(status, payload, (200, 201), f"create principal {name}")
        if payload is not None:
            creds = payload["credentials"]
            kube.write_secret(
                namespace,
                secret,
                {
                    "client-id": creds["clientId"],
                    "client-secret": creds["clientSecret"],
                    "credential": f"{creds['clientId']}:{creds['clientSecret']}",
                },
            )
            print(f"principal {name}: credentials written to {namespace}/{secret}")
        put(
            token,
            f"/principals/{name}/principal-roles",
            {"principalRole": {"name": role}},
            f"assign principal role {role} to {name}",
        )

    print("polaris setup complete")


if __name__ == "__main__":
    main()
