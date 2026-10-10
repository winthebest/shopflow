"""Copy a credential Secret from another namespace into this one, inside the cluster.

Database role passwords live in namespace `shop` (CNPG managed roles, SOPS-encrypted by their owner); pods read
Secrets only from their own namespace. This Job copies the keys instead of keeping a second encrypted copy in git
or decrypting with the age key (docs/contracts/gitops.md section 5), so a password change never drifts.

Env: SOURCE_NAMESPACE, SOURCE_NAME, TARGET_NAMESPACE, TARGET_NAME; optional EXTRA_DATA (JSON object of fixed keys
to add, e.g. a JDBC URL) and DERIVED_DATA (JSON object of keys built from the source's keys, e.g. a database URI:
"postgresql://{username}:{password}@host/db"; every substituted value is percent-encoded). Standard library only.
The same file exists in every component that needs a copy; scripts/data-validate.sh fails if the copies differ.
"""

import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc/api/v1"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    with open(f"{SA}/token") as f:
        token = f.read().strip()
    request = urllib.request.Request(  # noqa: S310 (fixed https URL of the API server)
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, {"message": error.read().decode(errors="replace")}


def main() -> None:
    env = os.environ
    source_ref = f"{env['SOURCE_NAMESPACE']}/{env['SOURCE_NAME']}"
    target_ref = f"{env['TARGET_NAMESPACE']}/{env['TARGET_NAME']}"
    status, source = call("GET", f"/namespaces/{env['SOURCE_NAMESPACE']}/secrets/{env['SOURCE_NAME']}")
    if status != 200:
        sys.exit(f"read {source_ref}: HTTP {status} {source.get('message')}")
    extra = json.loads(env.get("EXTRA_DATA", "{}"))
    derived = json.loads(env.get("DERIVED_DATA", "{}"))
    # Only text keys can feed a template. Binary keys (e.g. ca.p12 of a Strimzi cluster CA) are copied as they are and
    # never decoded; a template that names one fails with KeyError.
    values = {}
    for key, encoded in (source.get("data", {}).items() if derived else ()):
        try:
            values[key] = urllib.parse.quote(base64.b64decode(encoded).decode(), safe="")
        except UnicodeDecodeError:
            continue
    extra.update({k: template.format_map(values) for k, template in derived.items()})
    target = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": env["TARGET_NAME"],
            "namespace": env["TARGET_NAMESPACE"],
            "labels": {"app.kubernetes.io/managed-by": "copy-secret"},
            "annotations": {"shopflow.io/copied-from": source_ref},
        },
        "type": "Opaque",
        "data": source.get("data", {}),
        "stringData": extra,
    }
    collection = f"/namespaces/{env['TARGET_NAMESPACE']}/secrets"
    status, payload = call("POST", collection, target)
    if status == 409:
        status, payload = call("PUT", f"{collection}/{env['TARGET_NAME']}", target)
    if status not in (200, 201):
        sys.exit(f"write {target_ref}: HTTP {status} {payload.get('message')}")
    print(f"copied {source_ref} -> {target_ref}: keys {sorted([*source.get('data', {}), *extra])}")


if __name__ == "__main__":
    main()
