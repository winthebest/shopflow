#!/usr/bin/env bash
# Short breaks (< 4h) inside a session: scale the node group to zero and back. The control plane
# and the NLB keep billing (~$0.13/h), so never pause overnight: cloud-down instead.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/cloud-pause.sh [--dry-run] [--resume]

  (default)   record each node group's size in SSM and scale it to 0
  --resume    scale back to the recorded size (default 2) and wait for a Ready node

The lease keeps running while paused: a paused cluster past its lease is reaped like any other.
EOF
}

RESUME=0
DEFAULT_DESIRED="${CLOUD_RESUME_DESIRED:-2}"

nodegroup_field() {
  aws_ eks describe-nodegroup --cluster-name "$CLUSTER" --nodegroup-name "$1" --query "nodegroup.scalingConfig.$2" --output text
}

scale() {
  local ng="$1" desired="$2" max
  max="$(nodegroup_field "$ng" maxSize)"
  [ "$desired" -le "$max" ] || desired="$max"
  run aws_ eks update-nodegroup-config --cluster-name "$CLUSTER" --nodegroup-name "$ng" \
    --scaling-config "minSize=0,maxSize=$max,desiredSize=$desired" >/dev/null
  log "$ng -> $desired node(s)"
}

pause() {
  local ng desired saved="{}"
  for ng in $NODEGROUPS; do
    desired="$(nodegroup_field "$ng" desiredSize)"
    [ "$desired" = 0 ] || saved="$(jq -c --arg ng "$ng" --argjson n "$desired" '. + {($ng): $n}' <<<"$saved")"
  done
  [ "$saved" = "{}" ] || ssm_put "$SSM_RESUME" "$saved"
  for ng in $NODEGROUPS; do scale "$ng" 0; done
  local lease end="none (the reapers treat it as expired)"
  lease="$(lease_epoch)"
  [ -z "$lease" ] || end="$(iso_from_epoch "$lease")"
  warn "paused: control plane + NLB still bill ~\$0.13/h; lease ends $end. Do not keep it paused overnight."
}

resume() {
  local saved ng desired
  saved="$(ssm_get "$SSM_RESUME")"
  [ -n "$saved" ] || saved='{}'
  for ng in $NODEGROUPS; do
    desired="$(jq -r --arg ng "$ng" --arg d "$DEFAULT_DESIRED" '.[$ng] // ($d | tonumber)' <<<"$saved")"
    scale "$ng" "$desired"
  done
  update_kubeconfig
  wait_until 900 "a Ready node" nodes_ready || die "no node became Ready"
  ssm_delete "$SSM_RESUME"
  log "resumed. Kafka and Postgres reattach their EBS volumes in the same AZ."
}

main() {
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  while [ $# -gt 0 ]; do
    case "$1" in
      --resume) RESUME=1 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  require_cmds aws jq kubectl
  require_role "$OPERATOR_ROLE"
  [ "$(cluster_status)" = ACTIVE ] || die "cluster $CLUSTER is not ACTIVE"
  NODEGROUPS="$(aws_ eks list-nodegroups --cluster-name "$CLUSTER" --query 'nodegroups[]' --output text)"
  [ -n "$NODEGROUPS" ] || die "cluster $CLUSTER has no node group"
  if [ "$RESUME" = 1 ]; then resume; else pause; fi
}

main "$@"
