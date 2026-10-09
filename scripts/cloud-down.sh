#!/usr/bin/env bash
# End an AWS session gracefully and leave nothing billed by the hour. Every step checks the current
# state first, so running it again after an interruption picks up where it stopped:
#   1 auto-sync off -> 2 final backup + pointer -> 3 evidence -> 4 Gateway/LoadBalancer, wait for
#   ELBv2 -> 5 stateful CRs + PVCs, wait for EBS -> 6 uninstall Argo CD -> 7 tofu destroy layer 2 ->
#   8 orphan check in every region -> 9 drop lease, session and reaper schedule.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/cloud-down.sh [--dry-run] [--force-api]

  --dry-run    read state, run `tofu plan -destroy`, print every change instead of making it
  --force-api  skip the Kubernetes steps (no final backup, no evidence) and tear down through AWS
               APIs, like the reapers. For a paused or unreachable cluster. RPO = WAL archive_timeout.

Environment: AWS_PROFILE (default shopflow), CLOUD_KUBECONFIG, PG_BACKUP_METHOD, STATEFUL_KINDS.
EOF
}

FORCE_API=0
# Custom resources whose operators own PVCs; deleted before the PVCs so nothing re-creates them.
STATEFUL_KINDS="${STATEFUL_KINDS:-kafkas.kafka.strimzi.io kafkanodepools.kafka.strimzi.io clusters.postgresql.cnpg.io}"

parse_args() {
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  while [ $# -gt 0 ]; do
    case "$1" in
      --force-api) FORCE_API=1 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
}

has_resource_type() { kube get crd "$1" >/dev/null 2>&1; }

nodegroups_paused() {
  local ng desired total=0 paused=0
  for ng in $(aws_ eks list-nodegroups --cluster-name "$CLUSTER" --query 'nodegroups[]' --output text); do
    desired="$(aws_ eks describe-nodegroup --cluster-name "$CLUSTER" --nodegroup-name "$ng" --query 'nodegroup.scalingConfig.desiredSize' --output text)"
    total=$((total + 1))
    [ "$desired" != 0 ] || paused=$((paused + 1))
  done
  [ "$total" -gt 0 ] && [ "$paused" -eq "$total" ]
}

# ---- step 1 ------------------------------------------------------------------------------------

disable_autosync() {
  kube get namespace argocd >/dev/null 2>&1 || { log "Argo CD not installed; nothing to pause"; return 0; }
  local app
  # Root apps (sources under deploy/argocd/profiles/) first, so they cannot re-enable their children.
  for app in $(kube -n argocd get applications.argoproj.io -o json | jq -r '
      .items
      | map(select(.spec.syncPolicy.automated != null))
      | sort_by([.spec.source.path // "", (.spec.sources // [])[].path // ""] | map(startswith("deploy/argocd/profiles")) | any | not)
      | .[].metadata.name'); do
    run kube -n argocd patch applications.argoproj.io "$app" --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]' >&2
  done
}

# ---- step 2 ------------------------------------------------------------------------------------

psql_primary() {
  local primary
  primary="$(kube -n shop get clusters.postgresql.cnpg.io shop-db -o jsonpath='{.status.currentPrimary}')"
  kube -n shop exec "$primary" -c postgres -- psql -d shop -tAc "$1"
}

wal_archived() {
  local last
  last="$(psql_primary "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")"
  [ -n "$last" ] && { [ "$last" = "$1" ] || [[ "$last" > "$1" ]]; }
}

final_backup() {
  if ! kube -n shop get clusters.postgresql.cnpg.io shop-db >/dev/null 2>&1; then
    log "no shop-db cluster; nothing to back up"
    return 0
  fi
  local name="shop-db-$SESSION_ID-final" wal
  if backup_completed "$name"; then
    log "final backup $name already completed"
  elif dry_run; then
    run kube -n shop exec "<primary>" -c postgres -- psql -d shop -tAc "CHECKPOINT; SELECT pg_walfile_name(pg_switch_wal())"
  else
    # Close the current WAL segment and wait until it is archived, so the backup chain covers
    # every transaction committed before this point.
    wal="$(psql_primary "CHECKPOINT; SELECT pg_walfile_name(pg_switch_wal())" | tail -n 1)"
    wait_until 300 "WAL $wal archived" wal_archived "$wal" || die "WAL $wal was not archived; refusing to tear down without it"
  fi
  take_backup "$name"
}

# ---- step 4 ------------------------------------------------------------------------------------

no_cluster_load_balancers() { [ -z "$(cluster_load_balancers)" ]; }

remove_load_balancers() {
  if has_resource_type gateways.gateway.networking.k8s.io; then
    run kube delete gateways.gateway.networking.k8s.io --all --all-namespaces --wait=false >&2
  fi
  local svc
  for svc in $(kube get services --all-namespaces -o json | jq -r '.items[] | select(.spec.type == "LoadBalancer") | "\(.metadata.namespace)/\(.metadata.name)"'); do
    run kube -n "${svc%%/*}" delete service "${svc#*/}" --wait=false >&2
  done
  wait_until 600 "ELBv2 load balancers of $CLUSTER deleted" no_cluster_load_balancers ||
    die "load balancers still present; re-run cloud-down, or use --force-api"
}

# ---- step 5 ------------------------------------------------------------------------------------

no_persistent_volumes() { [ "$(kube get pv -o json | jq '.items | length')" = 0 ]; }

no_csi_volumes() {
  [ -z "$(aws_ ec2 describe-volumes --filters "Name=tag:project,Values=$PROJECT" "Name=tag-key,Values=ebs.csi.aws.com/cluster" \
    --query 'Volumes[].VolumeId' --output text)" ]
}

remove_stateful() {
  local kind
  for kind in $STATEFUL_KINDS; do
    has_resource_type "$kind" && run kube delete "$kind" --all --all-namespaces --wait=false >&2
  done
  run kube delete persistentvolumeclaims --all --all-namespaces --wait=false >&2
  wait_until 900 "persistent volumes released" no_persistent_volumes || die "persistent volumes remain; re-run cloud-down"
  wait_until 600 "EBS volumes of the session deleted" no_csi_volumes || die "EBS volumes remain; re-run cloud-down"
}

# ---- step 6 ------------------------------------------------------------------------------------

uninstall_argocd() {
  kube get namespace argocd >/dev/null 2>&1 || { log "Argo CD already gone"; return 0; }
  local app
  # Without finalizers, deleting the Applications does not cascade into every workload.
  for app in $(kube -n argocd get applications.argoproj.io -o name); do
    run kube -n argocd patch "$app" --type merge -p '{"metadata":{"finalizers":null}}' >&2
  done
  run kube -n argocd delete applications.argoproj.io --all --wait=false >&2
  if helm status argocd --namespace argocd --kubeconfig "$KUBECONFIG_FILE" >/dev/null 2>&1; then
    run helm uninstall argocd --namespace argocd --kubeconfig "$KUBECONFIG_FILE" --wait >&2
  fi
}

# ---- step 7 ------------------------------------------------------------------------------------

destroy_cluster_layer() {
  export_tofu_vars
  tofu_init cluster
  # The variable only matters for create/update; destroy just needs a valid value.
  local cidr
  cidr="$(jq -r '.operatorCidr // empty' <<<"$SESSION_JSON")"
  local vars=(-input=false -var "operator_cidr=${cidr:-127.0.0.1/32}")
  if dry_run; then
    tofu_layer cluster plan -destroy "${vars[@]}" >&2
    return 0
  fi
  local attempt
  for attempt in 1 2 3; do
    tofu_layer cluster destroy -auto-approve "${vars[@]}" >&2 && return 0
    warn "tofu destroy attempt $attempt failed"
    [ "$attempt" -eq 3 ] || sleep 60
  done
  die "tofu destroy failed 3 times; re-run cloud-down (it resumes) or check docs/runbooks/cloud-session.md"
}

api_teardown() {
  require_cmds uv
  log "tearing down through AWS APIs (no final backup: RPO is the WAL archive_timeout)"
  local args=(teardown --cluster "$CLUSTER" --project "$PROJECT" --keep-cluster --wait --timeout 1800)
  dry_run && args+=(--dry-run)
  reaper_cli "${args[@]}" || die "API teardown did not finish; re-run cloud-down --force-api"
}

main() {
  parse_args "$@"
  require_cmds aws tofu kubectl helm jq
  dry_run && log "DRY-RUN: reads only; changes are printed"

  require_role "$OPERATOR_ROLE"
  SESSION_JSON="$(ssm_get "$SSM_SESSION")"
  SESSION_JSON="${SESSION_JSON:-null}"
  SESSION_ID="$(jq -r '.id // empty' <<<"$SESSION_JSON")"
  SESSION_ID="${SESSION_ID:-unknown-$(date -u +%Y%m%dt%H%M%Sz)}"
  local status
  status="$(cluster_status)"
  log "session $SESSION_ID, cluster $CLUSTER: $status"

  if [ "$status" = ABSENT ]; then
    log "no cluster: skipping the Kubernetes steps"
  elif [ "$FORCE_API" = 1 ]; then
    api_teardown
  else
    if nodegroups_paused; then
      die "the node group is scaled to zero: run 'make cloud-resume' first (graceful, with final backup), or cloud-down --force-api"
    fi
    update_kubeconfig

    step 1 "turn off Argo CD auto-sync"
    disable_autosync
    step 2 "final backup and backup pointer"
    final_backup
    step 3 "export evidence"
    local evidence_args=(--session "$SESSION_ID")
    dry_run && evidence_args+=(--dry-run)
    "$REPO_ROOT/scripts/export-evidence.sh" "${evidence_args[@]}" || warn "evidence export incomplete; continuing teardown"
    step 4 "delete Gateways and LoadBalancer Services, wait for ELBv2"
    remove_load_balancers
    step 5 "delete stateful resources and PVCs, wait for EBS"
    remove_stateful
    step 6 "uninstall Argo CD"
    uninstall_argocd
  fi

  step 7 "tofu destroy layer 2"
  destroy_cluster_layer

  step 8 "orphan check in every region"
  local orphan_args=(--delete-tagged --alert) orphans=0
  dry_run && orphan_args+=(--dry-run)
  "$REPO_ROOT/scripts/aws-orphan-check.sh" "${orphan_args[@]}" || orphans=$?

  step 9 "drop lease, session and reaper schedule"
  ssm_delete "$SSM_LEASE"
  ssm_delete "$SSM_SESSION"
  ssm_delete "$SSM_RESUME"
  schedule_delete

  [ "$orphans" -eq 0 ] || die "orphan check exited $orphans: see the list above (tagged leftovers were deleted; untagged ones need a human)"
  if dry_run; then
    log "dry-run complete: nothing was changed"
  else
    log "session $SESSION_ID is down; nothing billed by the hour remains"
  fi
}

main "$@"
