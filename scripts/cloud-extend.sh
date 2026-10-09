#!/usr/bin/env bash
# Extend the session lease. Only SSM and the backup reaper schedule change; nothing in the cluster.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/cloud-extend.sh [--dry-run] [--hours N]

  --hours N   add N hours (default 2) to the current lease (or to now, if it already expired).
              The lease never ends more than lease.max_hours from now (infra/cloud-contract.json).
EOF
}

main() {
  local hours=2
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  while [ $# -gt 0 ]; do
    case "$1" in
      --hours) hours="${2:?--hours needs a value}"; shift ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  case "$hours" in '' | *[!0-9]*) die "--hours must be a whole number" ;; esac
  [ "$hours" -ge 1 ] || die "--hours must be at least 1"
  require_cmds aws jq
  require_role "$OPERATOR_ROLE"
  [ "$(cluster_status)" != ABSENT ] || die "no session cluster: use cloud-up"

  local now current base new cap grace
  now="$(now_epoch)"
  current="$(lease_epoch)"
  base="$now"
  if [ -n "$current" ] && [ "$current" -gt "$now" ]; then base="$current"; fi
  new=$((base + hours * 3600))
  cap=$((now + $(contract .lease.max_hours) * 3600))
  if [ "$new" -gt "$cap" ]; then
    warn "capping the lease at $(contract .lease.max_hours)h from now"
    new="$cap"
  fi
  grace="$(contract .reaper.grace_hours)"

  ssm_put "$SSM_LEASE" "$(iso_from_epoch "$new")"
  schedule_upsert "$(iso_from_epoch $((new + grace * 3600)))"
  log "lease ${current:+$(iso_from_epoch "$current") }-> $(iso_from_epoch "$new"); backup reaper from $(iso_from_epoch $((new + grace * 3600)))"
}

main "$@"
