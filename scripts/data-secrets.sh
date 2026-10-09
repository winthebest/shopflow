#!/usr/bin/env bash
# Generate sf-data's SOPS-encrypted Secrets (deploy/platform/<component>/local/secrets/*.enc.yaml).
# Values are random, generated and encrypted in a pipe: never printed, never written in plaintext. Encryption only
# needs the public age recipient in .sops.yaml; this script never decrypts and never reads the age key.
# Existing files are kept unless ROTATE=1 (then every value is new: the S3 identities and their client copies must
# change together, so rotation is all-or-nothing).
# Credentials owned by other lanes (CNPG role passwords) are copied in the cluster instead (secret-copy.yaml).
# Needs: sops, openssl, htpasswd (bcrypt for Trino's password file).
#
# --aws-json: print the origin Secrets an AWS cluster needs as JSON for SSM, and nothing else:
#   scripts/data-secrets.sh --aws-json | scripts/aws-seed-params.sh
# Only Trino's: on AWS the catalog is Glue (docs/adr/0506, no Polaris) and S3 uses Pod Identity (no SeaweedFS, no S3
# keys). trino-dbt, trino-exporter and trino-password-db form one group (the bcrypt hashes are of those passwords), so
# they are always printed and seeded together. Refuses a terminal as stdout. Needs openssl, htpasswd, jq.
set -euo pipefail

cd "$(dirname "$0")/.."

case "${1:-}" in
  "") AWS_JSON=0 ;;
  --aws-json) AWS_JSON=1 ;;
  *) echo "usage: scripts/data-secrets.sh [--aws-json]" >&2; exit 2 ;;
esac

# Every file this script owns; adding one here is the only way to add an sf-data Secret.
FILES=(
  deploy/platform/seaweedfs/local/secrets/seaweedfs-s3-config.enc.yaml
  deploy/platform/iceberg-catalog/local/secrets/polaris-storage.enc.yaml
  deploy/platform/iceberg-catalog/local/secrets/polaris-root.enc.yaml
  deploy/platform/trino/local/secrets/lake-s3-trino.enc.yaml
  deploy/platform/trino/local/secrets/lake-s3-trino-ro.enc.yaml
  deploy/platform/trino/local/secrets/trino-internal.enc.yaml
  deploy/platform/trino/local/secrets/trino-dbt.enc.yaml
  deploy/platform/trino/local/secrets/trino-exporter.enc.yaml
  deploy/platform/trino/local/secrets/trino-password-db.enc.yaml
  deploy/platform/kafka-connect/local/secrets/lake-s3-iceberg-sink.enc.yaml
)

hex() { openssl rand -hex "$1"; }

# Trino users with password login (rules.json). The password file holds bcrypt hashes only (cost 10, $2y$) of the
# same passwords as trino-dbt and trino-exporter: the three Secrets only work together.
trino_users() {
  dbt_password="$(hex 24)" exporter_password="$(hex 24)"
  password_db="$(htpasswd -niB -C 10 dbt <<< "$dbt_password")
$(htpasswd -niB -C 10 exporter <<< "$exporter_password")"
}

# JSON input of scripts/aws-seed-params.sh: {"_groups": [[...]], "<namespace>": {"<secret>": {"<key>": "<value>"}}}.
# Values reach jq through /dev/fd (process substitution), never through arguments.
if ((AWS_JSON)); then
  if [[ -t 1 ]]; then
    echo "--aws-json prints secrets: pipe it into scripts/aws-seed-params.sh instead of a terminal" >&2
    exit 1
  fi
  trino_users
  jq -n \
    --rawfile internal <(printf '%s' "$(hex 32)") \
    --rawfile dbt <(printf '%s' "$dbt_password") \
    --rawfile exporter <(printf '%s' "$exporter_password") \
    --rawfile password_db <(printf '%s' "$password_db") \
    '{_groups: [["lakehouse/trino-dbt", "lakehouse/trino-exporter", "lakehouse/trino-password-db"]],
      lakehouse: {
        "trino-internal": {"shared-secret": $internal},
        "trino-dbt": {username: "dbt", password: $dbt},
        "trino-exporter": {username: "exporter", password: $exporter},
        "trino-password-db": {"password.db": $password_db}}}'
  exit 0
fi

if [[ "${ROTATE:-0}" != "1" ]]; then
  for f in "${FILES[@]}"; do
    if [[ -e "$f" ]]; then
      echo "$f exists; nothing generated (ROTATE=1 regenerates every sf-data secret together)" >&2
      exit 1
    fi
  done
fi

# encrypt <path>: stdin is a Secret manifest; only data/stringData get encrypted (.sops.yaml encrypted_regex).
encrypt() {
  mkdir -p "$(dirname "$1")"
  sops --encrypt --filename-override "$1" --input-type yaml --output-type yaml /dev/stdin > "$1.tmp"
  mv "$1.tmp" "$1"
  echo "encrypted $1"
}

# secret <namespace> <name> <key> <value> [<key> <value> ...] -> Secret manifest on stdout.
secret() {
  local namespace="$1" name="$2"
  shift 2
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\n  labels:\n    app.kubernetes.io/part-of: shopflow\ntype: Opaque\nstringData:\n' \
    "$name" "$namespace"
  while (($#)); do
    # Literal block scalar: safe for any value (JSON, bcrypt hashes).
    printf '  %s: |-\n' "$1"
    # shellcheck disable=SC2001 # indents every line of a multi-line value
    sed 's/^/    /' <<< "$2"
    shift 2
  done
}

# One S3 identity per client (deploy/platform/seaweedfs/base/seaweedfs.yaml). Access keys are not secret by
# themselves but are generated with their secret keys.
polaris_key="$(hex 10)" polaris_secret="$(hex 20)"
sink_key="$(hex 10)" sink_secret="$(hex 20)"
trino_key="$(hex 10)" trino_secret="$(hex 20)"
trino_ro_key="$(hex 10)" trino_ro_secret="$(hex 20)"

s3_json="$(cat <<JSON
{"identities": [
  {"name": "polaris", "credentials": [{"accessKey": "$polaris_key", "secretKey": "$polaris_secret"}],
   "actions": ["Read:lake", "List:lake", "Write:lake"]},
  {"name": "iceberg-sink", "credentials": [{"accessKey": "$sink_key", "secretKey": "$sink_secret"}],
   "actions": ["Read:lake", "List:lake", "Write:lake"]},
  {"name": "trino-lake", "credentials": [{"accessKey": "$trino_key", "secretKey": "$trino_secret"}],
   "actions": ["Read:lake", "List:lake", "Write:lake"]},
  {"name": "trino-lake-ro", "credentials": [{"accessKey": "$trino_ro_key", "secretKey": "$trino_ro_secret"}],
   "actions": ["Read:lake", "List:lake"]}
]}
JSON
)"
secret lakehouse seaweedfs-s3-config s3.json "$s3_json" | encrypt "${FILES[0]}"
secret lakehouse polaris-storage access-key-id "$polaris_key" secret-access-key "$polaris_secret" | encrypt "${FILES[1]}"
secret lakehouse polaris-root client-id root client-secret "$(hex 24)" | encrypt "${FILES[2]}"
secret lakehouse lake-s3-trino access-key-id "$trino_key" secret-access-key "$trino_secret" | encrypt "${FILES[3]}"
secret lakehouse lake-s3-trino-ro access-key-id "$trino_ro_key" secret-access-key "$trino_ro_secret" \
  | encrypt "${FILES[4]}"
secret lakehouse trino-internal shared-secret "$(hex 32)" | encrypt "${FILES[5]}"

trino_users
secret lakehouse trino-dbt username dbt password "$dbt_password" | encrypt "${FILES[6]}"
secret lakehouse trino-exporter username exporter password "$exporter_password" | encrypt "${FILES[7]}"
secret lakehouse trino-password-db password.db "$password_db" | encrypt "${FILES[8]}"

secret kafka lake-s3-iceberg-sink access-key-id "$sink_key" secret-access-key "$sink_secret" | encrypt "${FILES[9]}"
