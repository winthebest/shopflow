"""Copy a credential Secret from another namespace into this one, inside the cluster.

Database role passwords live in namespace `shop` (CNPG managed roles, SOPS-encrypted by their owner); pods read
Secrets only from their own namespace. This Job copies the keys instead of keeping a second encrypted copy in git
or decrypting with the age key (docs/contracts/gitops.md section 5), so a password change never drifts.

Env: SOURCE_NAMESPACE, SOURCE_NAME, TARGET_NAMESPACE, TARGET_NAME; optional:
  EXTRA_DATA        JSON object of fixed keys to add, e.g. a JDBC URL;
  DERIVED_DATA      JSON object of keys built from the source's keys, e.g. "postgresql://{username}:{password}@host/db";
  DERIVED_ENCODING  how substituted values are encoded: `uri` (default, percent-encoded) or `json` (a quoted JSON
                    string, safe anywhere in a YAML or JSON document, e.g. a Grafana provisioning file);
  COPY_SOURCE_KEYS  `false` writes only EXTRA_DATA/DERIVED_DATA, not the source's own keys (default `true`);
  TARGET_LABELS     JSON object of labels for the target Secret (e.g. one a Grafana sidecar watches).
Standard library only.
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


ENCODERS = {"uri": lambda value: urllib.parse.quote(value, safe=""), "json": json.dumps}


def fill_templates(derived: dict[str, str], data: dict[str, str], encoding: str) -> dict[str, str]:
    """DERIVED_DATA filled from the source's base64 `data`, each value encoded for where it lands.

    Only text keys can feed a template. Binary keys (e.g. ca.p12 of a Strimzi cluster CA) are never decoded; a
    template that names one fails with KeyError.
    """
    if not derived:
        return {}
    encode = ENCODERS[encoding]
    values = {}
    for key, encoded in data.items():
        try:
            values[key] = encode(base64.b64decode(encoded).decode())
        except UnicodeDecodeError:
            continue
    return {key: template.format_map(values) for key, template in derived.items()}


def main() -> None:
    env = os.environ
    source_ref = f"{env['SOURCE_NAMESPACE']}/{env['SOURCE_NAME']}"
    target_ref = f"{env['TARGET_NAMESPACE']}/{env['TARGET_NAME']}"
    status, source = call("GET", f"/namespaces/{env['SOURCE_NAMESPACE']}/secrets/{env['SOURCE_NAME']}")
    if status != 200:
        sys.exit(f"read {source_ref}: HTTP {status} {source.get('message')}")
    encoding = env.get("DERIVED_ENCODING", "uri")
    if encoding not in ENCODERS:
        sys.exit(f"DERIVED_ENCODING={encoding!r}: expected one of {sorted(ENCODERS)}")
    extra = json.loads(env.get("EXTRA_DATA", "{}"))
    extra.update(fill_templates(json.loads(env.get("DERIVED_DATA", "{}")), source.get("data", {}), encoding))
    data = source.get("data", {}) if env.get("COPY_SOURCE_KEYS", "true") != "false" else {}
    target = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": env["TARGET_NAME"],
            "namespace": env["TARGET_NAMESPACE"],
            # TARGET_LABELS cannot override managed-by.
            "labels": {**json.loads(env.get("TARGET_LABELS", "{}")), "app.kubernetes.io/managed-by": "copy-secret"},
            "annotations": {"shopflow.io/copied-from": source_ref},
        },
        "type": "Opaque",
        "data": data,
        "stringData": extra,
    }
    collection = f"/namespaces/{env['TARGET_NAMESPACE']}/secrets"
    status, payload = call("POST", collection, target)
    if status == 409:
        status, payload = call("PUT", f"{collection}/{env['TARGET_NAME']}", target)
    if status not in (200, 201):
        sys.exit(f"write {target_ref}: HTTP {status} {payload.get('message')}")
    print(f"copied {source_ref} -> {target_ref}: keys {sorted([*data, *extra])}")


if __name__ == "__main__":
    main()
