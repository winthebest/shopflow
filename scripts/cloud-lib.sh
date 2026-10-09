#!/usr/bin/env bash
# Shared helpers for scripts/cloud-*.sh, aws-*.sh and export-evidence.sh. Source it; do not run it.
#
# Every constant comes from infra/cloud-contract.json, the file the OpenTofu layers also read.
# Dry-run (DRY_RUN=1, or --dry-run on each script): AWS and Kubernetes are still *read* so the output
# reflects the real state, but every change is printed instead of executed (`run`), OpenTofu runs
# `plan` instead of `apply`/`destroy`, and waits return immediately.
# Written for bash 3.2 (macOS /bin/bash) as well as bash 5 (GitHub runners).
# shellcheck disable=SC2034 # constants below are used by the scripts that source this file

CLOUD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$CLOUD_LIB_DIR/.." && pwd)"
CONTRACT="$REPO_ROOT/infra/cloud-contract.json"
TOFU_DIR="$REPO_ROOT/infra/tofu"
OUT_DIR="${CLOUD_OUT_DIR:-$REPO_ROOT/out/cloud}"
KUBECONFIG_FILE="${CLOUD_KUBECONFIG:-$HOME/.kube/shopflow-aws}"
SCRIPT_NAME="$(basename "${0:-cloud}" .sh)"
DRY_RUN="${DRY_RUN:-0}"
POLL_SECONDS="${CLOUD_POLL_SECONDS:-15}"

# Operator laptops use the Identity Center -> shopflow-operator profile; CI gets credentials from
# OIDC through the environment.
if [ -z "${GITHUB_ACTIONS:-}" ]; then
  export AWS_PROFILE="${AWS_PROFILE:-shopflow}"
fi
export AWS_PAGER=""

# ---- output ------------------------------------------------------------------------------------

log() { printf '%s [%s] %s\n' "$(date -u +%H:%M:%S)" "$SCRIPT_NAME" "$*" >&2; }
warn() { log "WARN: $*"; }
die() {
  log "ERROR: $*"
  exit 1
}
step() { log "== step $1: $2"; }
dry_run() { [ "$DRY_RUN" = 1 ]; }

# Print a command as it would be typed.
quote_cmd() {
  local out="" arg
  for arg in "$@"; do out="$out $(printf '%q' "$arg")"; done
  printf '%s' "${out# }"
}

# Run a command that changes something; in dry-run, print it instead.
run() {
  if dry_run; then
    printf 'DRY-RUN: %s\n' "$(quote_cmd "$@")" >&2
    return 0
  fi
  "$@"
}

require_cmds() {
  local cmd missing=""
  for cmd in "$@"; do command -v "$cmd" >/dev/null 2>&1 || missing="$missing $cmd"; done
  [ -z "$missing" ] || die "missing tools:$missing"
}

# ---- contract ----------------------------------------------------------------------------------

contract() { jq -er "$1" "$CONTRACT"; }

PROJECT="$(contract .project)"
REGION="$(contract .region)"
CLUSTER="$(contract .cluster_name)"
SSM_PREFIX="$(contract .ssm.prefix)"
SSM_LEASE="$(contract .ssm.lease)"
SSM_SESSION="$(contract .ssm.session)"
SSM_PG_POINTER="$(contract .ssm.pg_backup_pointer)"
SSM_PG_MARKER="$(contract .ssm.pg_initialized)"
SSM_RESUME="$(contract .ssm.resume_desired)"
SSM_CDC_EPOCH="$(contract .ssm.cdc_epoch)"
SSM_ARGOCD_ADMIN="$(contract .ssm.argocd_admin)"
PG_BACKUP_PREFIX="$(contract .data_prefixes.pg_backup)"
EVIDENCE_PREFIX="$(contract .data_prefixes.evidence)"
SCHEDULE_GROUP="$(contract .reaper.schedule_group)"
SCHEDULE_NAME="$(contract .reaper.schedule_name)"
OPERATOR_ROLE="$(contract .roles.operator)"

# ---- time (UTC; GNU and BSD date) --------------------------------------------------------------

now_epoch() { date -u +%s; }

iso_from_epoch() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ
}

# Accepts 2026-10-09T14:00:00Z and 2026-10-09T14:00:00+00:00; prints nothing when unparseable.
epoch_from_iso() {
  local value="$1"
  date -u -d "$value" +%s 2>/dev/null && return 0
  value="${value%Z}"
  value="${value%+00:00}"
  date -u -j -f %Y-%m-%dT%H:%M:%S "$value" +%s 2>/dev/null || true
}

# ---- AWS ---------------------------------------------------------------------------------------

aws_() { aws --region "$REGION" --output json "$@"; }

ACCOUNT_ID="${ACCOUNT_ID:-}"
account_id() {
  if [ -z "$ACCOUNT_ID" ]; then
    ACCOUNT_ID="$(aws_ sts get-caller-identity --query Account --output text)" || die "no AWS credentials (run: aws sso login --profile ${AWS_PROFILE:-shopflow})"
  fi
  printf '%s' "$ACCOUNT_ID"
}

# Fail unless the caller is the expected role (protects against running with an admin profile).
require_role() {
  local arn
  arn="$(aws_ sts get-caller-identity --query Arn --output text)" || die "no AWS credentials (run: aws sso login)"
  case "$arn" in
    *":assumed-role/$1/"*) ;;
    *) die "expected role $1, got $arn (set AWS_PROFILE to the $1 profile)" ;;
  esac
}

state_bucket() { printf '%s%s' "$(contract .state_bucket_prefix)" "$(account_id)"; }
data_bucket() { printf '%s%s' "$(contract .data_bucket_prefix)" "$(account_id)"; }
alert_topic_arn() { printf 'arn:aws:sns:%s:%s:%s-alerts' "$REGION" "$(account_id)" "$PROJECT"; }

# Read a parameter; prints nothing when it does not exist. Any other error stops the script.
ssm_get() {
  local out
  if out="$(aws_ ssm get-parameter --name "$1" --query Parameter.Value --output text 2>&1)"; then
    printf '%s' "$out"
    return 0
  fi
  case "$out" in *ParameterNotFound*) return 0 ;; esac
  die "ssm get-parameter $1 failed: $out"
}

# Control values only (lease, pointer, marker, session, epoch): plain String parameters, never
# secrets. Secrets go through aws-seed-params.sh as SecureString.
ssm_put() { run aws_ ssm put-parameter --name "$1" --type String --value "$2" --overwrite >/dev/null; }

# Create only if absent (marker semantics).
ssm_put_new() {
  local out
  if dry_run; then
    run aws_ ssm put-parameter --name "$1" --type String --value "$2"
    return 0
  fi
  if out="$(aws_ ssm put-parameter --name "$1" --type String --value "$2" 2>&1)"; then return 0; fi
  case "$out" in *ParameterAlreadyExists*) return 0 ;; esac
  die "ssm put-parameter $1 failed: $out"
}

ssm_delete() {
  [ -n "$(ssm_get "$1")" ] || return 0
  run aws_ ssm delete-parameter --name "$1"
}

# EKS status of the session cluster, or ABSENT.
cluster_status() {
  local out
  if out="$(aws_ eks describe-cluster --name "$CLUSTER" --query cluster.status --output text 2>&1)"; then
    printf '%s' "$out"
    return 0
  fi
  case "$out" in *ResourceNotFoundException*) printf 'ABSENT' ;; *) die "describe-cluster failed: $out" ;; esac
}

# ARNs of ELBv2 load balancers the LB controller created for this cluster.
cluster_load_balancers() {
  local arns
  arns="$(aws_ elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerArn' --output text)" || die "describe-load-balancers failed"
  [ -n "$arns" ] && [ "$arns" != None ] || return 0
  # shellcheck disable=SC2086 # split the whitespace-separated ARN list on purpose
  set -- $arns
  while [ $# -gt 0 ]; do
    local batch=() i=0
    while [ $# -gt 0 ] && [ $i -lt 20 ]; do
      batch+=("$1")
      shift
      i=$((i + 1))
    done
    aws_ elbv2 describe-tags --resource-arns "${batch[@]}" |
      jq -r --arg c "$CLUSTER" '.TagDescriptions[] | select(any(.Tags[]?; .Key == "elbv2.k8s.aws/cluster" and .Value == $c)) | .ResourceArn'
  done
}

# The per-session schedule of the backup reaper: every few minutes from <start> until it removes itself.
schedule_upsert() {
  local start="$1" account target role
  account="$(account_id)"
  target="arn:aws:lambda:$REGION:$account:function:$(contract .reaper.function_name)"
  role="arn:aws:iam::$account:role/$(contract .roles.reaper_scheduler)"
  local args=(
    --group-name "$SCHEDULE_GROUP" --name "$SCHEDULE_NAME"
    --schedule-expression "$(contract .reaper.schedule_rate)" --start-date "$start"
    --flexible-time-window Mode=OFF --state ENABLED
    --target "{\"Arn\":\"$target\",\"RoleArn\":\"$role\",\"Input\":\"{}\"}"
  )
  if aws_ scheduler get-schedule --group-name "$SCHEDULE_GROUP" --name "$SCHEDULE_NAME" >/dev/null 2>&1; then
    run aws_ scheduler update-schedule "${args[@]}" >/dev/null
  else
    run aws_ scheduler create-schedule "${args[@]}" >/dev/null
  fi
}

schedule_delete() {
  aws_ scheduler get-schedule --group-name "$SCHEDULE_GROUP" --name "$SCHEDULE_NAME" >/dev/null 2>&1 || return 0
  run aws_ scheduler delete-schedule --group-name "$SCHEDULE_GROUP" --name "$SCHEDULE_NAME"
}

# Lease expiry as epoch seconds; empty when missing or unreadable (both count as expired).
lease_epoch() {
  local value
  value="$(ssm_get "$SSM_LEASE")"
  [ -n "$value" ] || return 0
  epoch_from_iso "$value"
}

# The reaper's teardown logic (infra/lambda/reaper) as a CLI: same code as the Lambda.
reaper_cli() {
  PYTHONPATH="$REPO_ROOT/infra/lambda/reaper/src" uv run --quiet --project "$REPO_ROOT/infra" python -m reaper --region "$REGION" "$@"
}

# ---- OpenTofu ----------------------------------------------------------------------------------

tofu_layer() {
  local layer="$1"
  shift
  tofu -chdir="$TOFU_DIR/$layer" "$@"
}

tofu_init() {
  tofu_layer "$1" init -input=false -reconfigure -backend-config="bucket=$(state_bucket)" >&2
}

export_tofu_vars() {
  TF_VAR_aws_account_id="$(account_id)"
  export TF_VAR_aws_account_id
}

# ---- Kubernetes --------------------------------------------------------------------------------

kube() { kubectl --kubeconfig "$KUBECONFIG_FILE" "$@"; }

update_kubeconfig() {
  mkdir -p "$(dirname "$KUBECONFIG_FILE")"
  aws_ eks update-kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG_FILE" --alias "$PROJECT-aws" >&2
}

nodes_ready() {
  kube get nodes -o json 2>/dev/null |
    jq -e '[.items[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))] | length > 0' >/dev/null
}

# ---- waiting -----------------------------------------------------------------------------------

# wait_until <timeout-seconds> <description> <command...>
wait_until() {
  local timeout="$1" desc="$2"
  shift 2
  if dry_run; then
    log "dry-run: would wait up to ${timeout}s for $desc"
    return 0
  fi
  local deadline=$(($(now_epoch) + timeout))
  until "$@"; do
    if [ "$(now_epoch)" -ge "$deadline" ]; then
      warn "timed out after ${timeout}s waiting for $desc"
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
  log "ok: $desc"
}

# ---- Postgres backup chain ---------------------------------------------------------------------

# Barman layout under s3://<data>/pg-backup/<serverName>/base/<backupId>/backup.info.
backup_exists() {
  aws_ s3api head-object --bucket "$(data_bucket)" --key "${PG_BACKUP_PREFIX}$1/base/$2/backup.info" >/dev/null 2>&1
}

cnpg_backup_phase() {
  kube -n shop get backups.postgresql.cnpg.io "$1" -o jsonpath='{.status.phase}' 2>/dev/null || true
}

backup_completed() { [ "$(cnpg_backup_phase "$1")" = completed ]; }

# On-demand CNPG Backup -> wait until completed -> move the SSM pointer to it. Idempotent: an
# already-completed Backup of the same name only refreshes the pointer.
take_backup() {
  local name="$1" file
  if ! backup_completed "$name"; then
    mkdir -p "$OUT_DIR"
    file="$OUT_DIR/backup-$name.yaml"
    if [ "${PG_BACKUP_METHOD:-plugin}" = plugin ]; then
      cat >"$file" <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: $name
  namespace: shop
  labels:
    app.kubernetes.io/part-of: $PROJECT
spec:
  cluster:
    name: shop-db
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
EOF
    else
      cat >"$file" <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: $name
  namespace: shop
  labels:
    app.kubernetes.io/part-of: $PROJECT
spec:
  cluster:
    name: shop-db
  method: barmanObjectStore
EOF
    fi
    run kube apply -f "$file" >&2
    dry_run && return 0
    wait_until 1800 "Backup $name completed" backup_settled "$name" || die "Backup $name did not finish"
    [ "$(cnpg_backup_phase "$name")" = completed ] || die "Backup $name failed: $(kube -n shop get backups.postgresql.cnpg.io "$name" -o jsonpath='{.status.error}')"
  fi
  local pointer
  pointer="$(kube -n shop get backups.postgresql.cnpg.io "$name" -o json | jq -ce '{serverName: .status.serverName, backupId: .status.backupId}')" ||
    die "Backup $name has no serverName/backupId in its status"
  ssm_put "$SSM_PG_POINTER" "$pointer"
  log "backup pointer -> $pointer"
}

backup_settled() {
  case "$(cnpg_backup_phase "$1")" in completed | failed) return 0 ;; *) return 1 ;; esac
}

# Parse --dry-run / --help shared by every script; sets DRY_RUN and leaves other args in ARGS.
ARGS=()
parse_common_args() {
  ARGS=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      -h | --help)
        usage
        exit 0
        ;;
      *) ARGS+=("$1") ;;
    esac
    shift
  done
}
