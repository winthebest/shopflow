#!/usr/bin/env bash
# Seed application secrets into SSM Parameter Store as SecureString (default aws/ssm key), where
# External Secrets reads them: /shopflow/aws/<namespace>/<name>.
#
# Input is a JSON object on stdin, never arguments (arguments show up in `ps` and shell history):
#   {"shop": {"db-app-password": "..."}, "kafka": {"connect-scram-password": "..."}}
# Idempotent: existing parameters are left alone unless --rotate, which overwrites the ones given.
# Values are never printed and never passed on a command line.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/aws-seed-params.sh [--dry-run] [--rotate] < secrets.json

  --rotate   overwrite parameters that already exist (default: keep them)
Namespaces must be listed in eso_namespaces of infra/cloud-contract.json.
EOF
}

parameter_exists() {
  [ "$(aws_ ssm describe-parameters --parameter-filters "Key=Name,Values=$1" --query 'length(Parameters)' --output text)" != 0 ]
}

main() {
  local rotate=0
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  while [ $# -gt 0 ]; do
    case "$1" in
      --rotate) rotate=1 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  require_cmds aws jq
  [ ! -t 0 ] || die "pipe the secrets JSON on stdin (see --help)"
  local input
  input="$(cat)"
  jq -e 'type == "object" and all(.[]; type == "object" and all(.[]; type == "string" and length > 0))' <<<"$input" >/dev/null ||
    die "stdin must be {\"<namespace>\": {\"<name>\": \"<non-empty value>\"}}"
  local bad
  bad="$(jq -r --argjson allowed "$(jq -c .eso_namespaces "$CONTRACT")" \
    'to_entries[] | .key as $ns | if ($allowed | any(. == $ns)) | not then "namespace \($ns) is not in eso_namespaces" else (.value | keys[] | select(test("^[A-Za-z0-9_.-]+$") | not) | "name \($ns)/\(.) has invalid characters") end' <<<"$input")"
  [ -z "$bad" ] || die "$bad"

  require_role "$OPERATOR_ROLE"
  local ns name path created=0 rotated=0 kept=0
  while IFS=$'\t' read -r ns name; do
    path="$SSM_PREFIX/$ns/$name"
    if parameter_exists "$path"; then
      if [ "$rotate" = 0 ]; then
        log "keep    $path (exists; --rotate to overwrite)"
        kept=$((kept + 1))
        continue
      fi
      if dry_run; then log "DRY-RUN: would overwrite $path (SecureString)"; else
        jq -c --arg ns "$ns" --arg name "$name" --arg path "$path" \
          '{Name: $path, Value: .[$ns][$name], Type: "SecureString", KeyId: "alias/aws/ssm", Overwrite: true}' <<<"$input" |
          aws_ ssm put-parameter --cli-input-json file:///dev/stdin >/dev/null
      fi
      log "rotated $path"
      rotated=$((rotated + 1))
    else
      if dry_run; then log "DRY-RUN: would create $path (SecureString)"; else
        jq -c --arg ns "$ns" --arg name "$name" --arg path "$path" --arg project "$PROJECT" \
          '{Name: $path, Value: .[$ns][$name], Type: "SecureString", KeyId: "alias/aws/ssm", Tags: [{Key: "project", Value: $project}]}' <<<"$input" |
          aws_ ssm put-parameter --cli-input-json file:///dev/stdin >/dev/null
      fi
      log "created $path"
      created=$((created + 1))
    fi
  done < <(jq -r 'to_entries[] | .key as $ns | .value | keys[] | [$ns, .] | @tsv' <<<"$input")
  log "done: $created created, $rotated rotated, $kept kept"
}

main "$@"
