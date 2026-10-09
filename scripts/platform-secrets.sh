#!/usr/bin/env bash
# Origin Secrets of sf-platform components on AWS, printed as JSON for SSM, and nothing else:
#   scripts/platform-secrets.sh --aws-json | scripts/aws-seed-params.sh
#
# Today: one Secret per shop-db login role enabled in the chart's aws values (deploy/charts/shop-db), with the role's
# username and a random password, in the shape the chart's ExternalSecrets read
# (/shopflow/aws/shop/<secret> = {"username", "password"}). Locally the same Secrets come from SOPS.
#
# Passwords must stay stable: the roles come back with the database restored every session, so a changed SSM value
# would no longer match the role. Seeding keeps parameters that already exist, so running this again does not
# change a seeded password. Rotate only on purpose (seed rotation plus a matching ALTER ROLE).
#
# Prints secrets: refuses a terminal as stdout. Values reach jq through /dev/fd, never through arguments.
# Needs: yq, jq, openssl.
set -euo pipefail

cd "$(dirname "$0")/.."

[[ "${1:-}" == "--aws-json" && $# -eq 1 ]] \
  || { echo "usage: scripts/platform-secrets.sh --aws-json | scripts/aws-seed-params.sh" >&2; exit 2; }
if [[ -t 1 ]]; then
  echo "--aws-json prints secrets: pipe it into scripts/aws-seed-params.sh instead of a terminal" >&2
  exit 1
fi

CHART=deploy/charts/shop-db

# Roles enabled on AWS, as "<role> <secret>" lines: the chart's values.yaml merged with values-aws.yaml.
# shellcheck disable=SC2016 # $f is a yq variable
roles="$(yq ea '. as $f ireduce ({}; . * $f) | .roles | to_entries[] | select(.value.enabled) | .key + " " + .value.secret' \
  "$CHART/values.yaml" "$CHART/values-aws.yaml")"
[[ -n "$roles" ]] || { echo "no enabled roles in $CHART values-aws" >&2; exit 1; }

json='{}'
while read -r role secret; do
  json="$(jq -c --arg secret "$secret" --arg role "$role" --rawfile password <(printf '%s' "$(openssl rand -hex 24)") \
    '.shop[$secret] = {username: $role, password: $password}' <<<"$json")"
done <<<"$roles"
printf '%s\n' "$json"
