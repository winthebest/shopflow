#!/usr/bin/env bash
# The GitHub reaper (.github/workflows/cloud-reaper.yml): destroy an expired session without the
# operator, using AWS APIs only, so it works while the node group is paused or the EKS API is closed.
#   1 take the layer-2 state lock (same S3 lockfile OpenTofu uses) -> nobody applies meanwhile
#   2 re-read the lease *after* holding the lock; a missing lease counts as expired
#   3 delete NLBs by tag -> node groups -> available EBS (reaper CLI, same code as the Lambda)
#   4 tofu destroy layer 2 under our lock (-lock=false) -> orphan check -> release the lock
# There is no final backup on this path: RPO is the WAL archive_timeout (default 5 minutes).
# Exit non-zero (= alert through the failed workflow) when it fails, when the lock is stale, or
# when the cluster outlives its lease by more than the grace period.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/cloud-reap.sh [--dry-run]

Environment: REAPER_LOCK_STALE_HOURS (default 3).
EOF
}

LOCK_STALE_SECONDS=$((${REAPER_LOCK_STALE_HOURS:-3} * 3600))
LOCK_KEY="$(contract .state_keys.cluster).tflock"
LOCK_HELD=0

release_lock() {
  [ "$LOCK_HELD" = 1 ] || return 0
  if aws_ s3api delete-object --bucket "$(state_bucket)" --key "$LOCK_KEY" >/dev/null; then
    log "released the state lock"
  else
    warn "could not release s3://$(state_bucket)/$LOCK_KEY; remove it by hand after checking no run is active"
  fi
}

# Conditional create (If-None-Match: *) is exactly how OpenTofu's S3 lockfile works, so a running
# cloud-up/cloud-down and this reaper exclude each other.
acquire_lock() {
  local info out
  info="$OUT_DIR/reaper-lock.json"
  mkdir -p "$OUT_DIR"
  jq -n --arg id "reaper-${GITHUB_RUN_ID:-local}-$$" --arg created "$(iso_from_epoch "$(now_epoch)")" --arg path "$(contract .state_keys.cluster)" \
    '{ID: $id, Operation: "OperationTypeApply", Info: "cloud-reaper", Who: "github-actions", Version: "", Created: $created, Path: $path}' >"$info"
  if dry_run; then
    run aws_ s3api put-object --bucket "$(state_bucket)" --key "$LOCK_KEY" --body "$info" --if-none-match '*'
    return 0
  fi
  if out="$(aws_ s3api put-object --bucket "$(state_bucket)" --key "$LOCK_KEY" --body "$info" --if-none-match '*' 2>&1)"; then
    LOCK_HELD=1
    log "holding the layer-2 state lock"
    return 0
  fi
  case "$out" in
    *PreconditionFailed* | *ConditionalRequestConflict* | *412*) return 1 ;;
    *) die "could not take the state lock: $out" ;;
  esac
}

lock_age_seconds() {
  local modified
  modified="$(aws_ s3api head-object --bucket "$(state_bucket)" --key "$LOCK_KEY" --query LastModified --output text)" || return 1
  printf '%s' $(($(now_epoch) - $(epoch_from_iso "$modified")))
}

# Seconds the cluster has outlived its lease (0 when the lease is valid; absent lease = since forever).
overdue_seconds() {
  local lease now
  lease="$(lease_epoch)"
  now="$(now_epoch)"
  if [ -z "$lease" ]; then printf '%s' "$now"; elif [ "$lease" -lt "$now" ]; then printf '%s' $((now - lease)); else printf 0; fi
}

main() {
  parse_common_args "$@"
  [ ${#ARGS[@]} -eq 0 ] || { usage >&2; die "unknown argument: ${ARGS[0]}"; }
  require_cmds aws jq tofu uv
  require_role "$(contract .roles.reaper)"
  trap release_lock EXIT

  local status grace overdue
  status="$(cluster_status)"
  grace=$(($(contract .reaper.grace_hours) * 3600))
  if [ "$status" = ABSENT ] && [ -z "$(cluster_load_balancers)" ]; then
    # Still look for leftovers every hour (e.g. an untagged load balancer cloud-down could not remove): a
    # failing run is the alert.
    log "no session cluster and no load balancers: nothing to reap; checking for orphans"
    "$REPO_ROOT/scripts/aws-orphan-check.sh"
    return 0
  fi

  if ! acquire_lock; then
    local age
    age="$(lock_age_seconds || echo 0)"
    overdue="$(overdue_seconds)"
    [ "$age" -lt "$LOCK_STALE_SECONDS" ] || die "the layer-2 state lock is ${age}s old: a run died holding it; check and remove s3://$(state_bucket)/$LOCK_KEY"
    [ "$overdue" -le "$grace" ] || die "cluster $status is ${overdue}s past its lease and the state is locked (${age}s): investigate now"
    log "state is locked by another run (${age}s); trying again next hour"
    return 0
  fi

  # Only now, holding the lock, is the lease decision safe from a concurrent cloud-up/extend.
  local lease_code=0
  reaper_cli lease --parameter "$SSM_LEASE" || lease_code=$?
  case "$lease_code" in
    0) log "lease expired or missing: reaping $CLUSTER ($status)" ;;
    10)
      log "lease still valid: nothing to do"
      return 0
      ;;
    *) die "could not read the lease (exit $lease_code)" ;;
  esac

  local teardown=(teardown --cluster "$CLUSTER" --project "$PROJECT" --keep-cluster --wait --timeout 2400)
  dry_run && teardown+=(--dry-run)
  reaper_cli "${teardown[@]}" || die "API teardown did not finish"

  export_tofu_vars
  tofu_init cluster
  local vars=(-input=false -lock=false -var "operator_cidr=127.0.0.1/32")
  if dry_run; then
    tofu_layer cluster plan -destroy "${vars[@]}" >&2
  else
    local attempt destroyed=0
    for attempt in 1 2 3; do
      if tofu_layer cluster destroy -auto-approve "${vars[@]}" >&2; then
        destroyed=1
        break
      fi
      warn "tofu destroy attempt $attempt failed"
      [ "$attempt" -eq 3 ] || sleep 60
    done
    [ "$destroyed" = 1 ] || die "tofu destroy failed 3 times"
  fi

  local orphan_args=(--delete-tagged)
  dry_run && orphan_args+=(--dry-run)
  "$REPO_ROOT/scripts/aws-orphan-check.sh" "${orphan_args[@]}"
  log "session reaped"
}

main "$@"
