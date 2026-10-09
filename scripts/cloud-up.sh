#!/usr/bin/env bash
# Start an AWS session: apply layer 2, deploy the same GitOps manifests with the aws overlay,
# restore Postgres along the backup chain, smoke test, then arm the lease and both kill switches.
# No manual step between start and the printed RTO. Re-runnable with --resume after a failure.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/cloud-up.sh [--dry-run] [--resume] [--hours N] [--pitr TIME] [--profiles LIST] [--revision REV]

  --dry-run        read AWS state, run `tofu plan`, print every change instead of making it
  --resume         continue a session whose cloud-up stopped half-way (reuses its recorded plan)
  --hours N        lease length in hours (default: contract lease.default_hours, max lease.max_hours)
  --pitr TIME      restore Postgres to this RFC 3339 time instead of the end of the last backup chain
  --profiles LIST  Argo CD profiles (default: $CLOUD_PROFILES or core,obs,data,rt,batch,bi)
  --revision REV   git revision Argo CD deploys (default: main)

Environment: AWS_PROFILE (default shopflow), CLOUD_KUBECONFIG, ROOT_APPS_HOOK, CDC_EPOCH_HOOK,
ARGOCD_CHART_FILE, ARGOCD_VALUES, ARGOCD_AWS_VALUES (Argo CD chart and values, sf-platform),
PG_BACKUP_METHOD (plugin|barmanObjectStore), SMOKE_CUSTOMER_ID, BRONZE_ORDERS_TABLE (in catalog TRINO_CATALOG),
TRINO_USER, TRINO_PASSWORD_SECRET (read-only Trino identity for the smoke query, sf-data).
Hooks get the session kubeconfig through KUBECONFIG.
EOF
}

ARGOCD_CHART_FILE="${ARGOCD_CHART_FILE:-$REPO_ROOT/deploy/argocd/bootstrap/argocd-chart.yaml}"
ARGOCD_VALUES="${ARGOCD_VALUES:-$REPO_ROOT/deploy/argocd/bootstrap/values.yaml}"
ARGOCD_AWS_VALUES="${ARGOCD_AWS_VALUES:-$REPO_ROOT/deploy/argocd/bootstrap/values-aws.yaml}"
ROOT_APPS_HOOK="${ROOT_APPS_HOOK:-$REPO_ROOT/scripts/platform-root-apps.sh}"
CDC_EPOCH_HOOK="${CDC_EPOCH_HOOK:-$REPO_ROOT/scripts/cdc-epoch.sh}"
# Smoke query identity: read-only catalog and user (sf-data contract); the password never leaves stdin.
TRINO_CATALOG="${TRINO_CATALOG:-lake_ro}"
TRINO_USER="${TRINO_USER:-exporter}"
TRINO_PASSWORD_SECRET="${TRINO_PASSWORD_SECRET:-trino-exporter}"
BRONZE_ORDERS_TABLE="${BRONZE_ORDERS_TABLE:-bronze.orders}"
SMOKE_CUSTOMER_ID="${SMOKE_CUSTOMER_ID:-1}"

RESUME=0
HOURS="$(contract .lease.default_hours)"
PITR=""
PROFILES="${CLOUD_PROFILES:-core,obs,data,rt,batch,bi}"
REVISION="main"

parse_args() {
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  while [ $# -gt 0 ]; do
    case "$1" in
      --resume) RESUME=1 ;;
      --hours) HOURS="${2:?--hours needs a value}"; shift ;;
      --pitr) PITR="${2:?--pitr needs a value}"; shift ;;
      --profiles) PROFILES="${2:?--profiles needs a value}"; shift ;;
      --revision) REVISION="${2:?--revision needs a value}"; shift ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  case "$HOURS" in '' | *[!0-9]*) die "--hours must be a whole number" ;; esac
  [ "$HOURS" -ge 1 ] && [ "$HOURS" -le "$(contract .lease.max_hours)" ] || die "--hours must be 1..$(contract .lease.max_hours)"
}

# ---- step 1: preflight ---------------------------------------------------------------------------

check_budget_action() {
  local statuses
  statuses="$(aws_ budgets describe-budget-actions-for-budget --account-id "$(account_id)" --budget-name "$PROJECT-total" \
    --query 'Actions[].Status' --output text)" || die "cannot read the Budget Action (is layer 0 applied?)"
  case "$statuses" in
    *EXECUTION_SUCCESS* | *EXECUTION_IN_PROGRESS* | *PENDING*)
      die "the Budget Action has fired ($statuses): new capacity is denied. Review docs/cost.md, then reset the action deliberately."
      ;;
  esac
  log "budget action status: ${statuses:-none}"
}

check_kubernetes_version() {
  local version status
  version="$(contract .kubernetes_version)"
  status="$(aws_ eks describe-cluster-versions --cluster-versions "$version" --query 'clusterVersions[0].versionStatus' --output text)" ||
    die "cannot check EKS version $version"
  [ "$status" = STANDARD_SUPPORT ] || die "EKS $version is $status; extended support costs \$0.60/h. Bump kubernetes_version in infra/cloud-contract.json."
  log "EKS $version is in standard support"
}

detect_operator_cidr() {
  local ip
  ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')" || die "cannot detect the public IP"
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "unexpected public IP answer: $ip"
  printf '%s/32' "$ip"
}

# Decide how Postgres starts, failing closed (ADR 0511):
#   pointer present  -> recover from it (the backup must exist in S3)
#   no pointer, no marker -> first session: initdb
#   marker present, pointer missing -> stop; never initdb over a database that existed
plan_postgres() {
  local pointer marker server backup
  pointer="$(ssm_get "$SSM_PG_POINTER")"
  marker="$(ssm_get "$SSM_PG_MARKER")"
  if [ -n "$pointer" ]; then
    if ! server="$(jq -er .serverName <<<"$pointer" 2>/dev/null)" || ! backup="$(jq -er .backupId <<<"$pointer" 2>/dev/null)"; then
      die "backup pointer $SSM_PG_POINTER is unreadable: refusing to start (docs/runbooks/cloud-session.md#backup-chain)"
    fi
    backup_exists "$server" "$backup" ||
      die "backup pointer -> $server/$backup, but s3://$(data_bucket)/${PG_BACKUP_PREFIX}$server/base/$backup/backup.info is missing: refusing to start"
    PG_MODE=recovery
    PG_RECOVERY_FROM="$server"
  elif [ -n "$marker" ]; then
    die "Postgres was initialised before ($SSM_PG_MARKER=$marker) but the backup pointer is missing: refusing initdb, it would start an empty database (docs/runbooks/cloud-session.md#backup-chain)"
  else
    [ -z "$PITR" ] || die "--pitr needs an existing backup chain"
    PG_MODE=initdb
    PG_RECOVERY_FROM=""
  fi
  log "postgres: $PG_MODE${PG_RECOVERY_FROM:+ from $PG_RECOVERY_FROM}${PITR:+ to $PITR}"
}

next_cdc_epoch() {
  local last
  last="$(ssm_get "$SSM_CDC_EPOCH")"
  case "$last" in '') printf 1 ;; *[!0-9]*) die "$SSM_CDC_EPOCH is not an integer: $last" ;; *) printf '%s' $((last + 1)) ;; esac
}

new_session() {
  local lease now
  now="$(now_epoch)"
  lease="$(lease_epoch)"
  if [ -n "$lease" ] && [ "$lease" -gt "$now" ]; then
    die "a session is active until $(iso_from_epoch "$lease"): use cloud-extend, cloud-up --resume, or cloud-down"
  fi
  [ "$(cluster_status)" = ABSENT ] || die "cluster $CLUSTER exists without a valid lease: run cloud-down first"

  plan_postgres
  SESSION_ID="$(date -u +%Y%m%dt%H%M%Sz)"
  PG_SERVER_NAME="shop-db-$SESSION_ID"
  CDC_EPOCH="$(next_cdc_epoch)"
  OPERATOR_CIDR="$(detect_operator_cidr)"
  SESSION_JSON="$(jq -cn --arg id "$SESSION_ID" --arg mode "$PG_MODE" --arg from "$PG_RECOVERY_FROM" --arg server "$PG_SERVER_NAME" \
    --arg pitr "$PITR" --arg epoch "$CDC_EPOCH" --arg cidr "$OPERATOR_CIDR" --arg profiles "$PROFILES" --arg rev "$REVISION" \
    '{id: $id, pg: {mode: $mode, recoveryFrom: $from, serverName: $server, recoveryTargetTime: $pitr}, cdcEpoch: ($epoch | tonumber), operatorCidr: $cidr, profiles: $profiles, revision: $rev}')"

  # A provisional lease first: without it the reapers would treat the half-built cluster as expired.
  write_lease
  ssm_put "$SSM_SESSION" "$SESSION_JSON"
  ssm_put "$SSM_CDC_EPOCH" "$CDC_EPOCH"
}

resume_session() {
  SESSION_JSON="$(ssm_get "$SSM_SESSION")"
  [ -n "$SESSION_JSON" ] || die "no recorded session to resume ($SSM_SESSION)"
  SESSION_ID="$(jq -er .id <<<"$SESSION_JSON")"
  PG_MODE="$(jq -er .pg.mode <<<"$SESSION_JSON")"
  PG_RECOVERY_FROM="$(jq -r .pg.recoveryFrom <<<"$SESSION_JSON")"
  PG_SERVER_NAME="$(jq -er .pg.serverName <<<"$SESSION_JSON")"
  PITR="$(jq -r .pg.recoveryTargetTime <<<"$SESSION_JSON")"
  CDC_EPOCH="$(jq -er .cdcEpoch <<<"$SESSION_JSON")"
  PROFILES="$(jq -er .profiles <<<"$SESSION_JSON")"
  REVISION="$(jq -er .revision <<<"$SESSION_JSON")"
  OPERATOR_CIDR="$(detect_operator_cidr)"
  write_lease
  log "resuming session $SESSION_ID"
}

write_lease() {
  LEASE_ISO="$(iso_from_epoch $(($(now_epoch) + HOURS * 3600)))"
  ssm_put "$SSM_LEASE" "$LEASE_ISO"
  log "lease until $LEASE_ISO"
}

# ---- step 2–5: infrastructure and GitOps ---------------------------------------------------------

apply_cluster_layer() {
  export_tofu_vars
  tofu_init cluster
  local vars=(-input=false -var "operator_cidr=$OPERATOR_CIDR")
  if dry_run; then
    tofu_layer cluster plan "${vars[@]}" >&2
  else
    tofu_layer cluster apply -auto-approve "${vars[@]}" >&2
  fi
}

# Argo CD admin password (ADR 0205: never the chart's random initial secret). Created once, generated inside a
# pipe straight into SSM as SecureString: nobody types or sees it, and it never touches argv or a file.
# scripts/cloud-argocd.sh password copies it to the clipboard.
ensure_argocd_admin_password() {
  local exists
  exists="$(aws_ ssm describe-parameters --parameter-filters "Key=Name,Values=$SSM_ARGOCD_ADMIN" \
    --query 'length(Parameters)' --output text)" || die "cannot read $SSM_ARGOCD_ADMIN"
  [ "$exists" = 0 ] || return 0
  if dry_run; then
    log "DRY-RUN: would generate the Argo CD admin password into $SSM_ARGOCD_ADMIN (SecureString)"
    return 0
  fi
  openssl rand -base64 24 | tr -d '\n' |
    jq -Rs --arg name "$SSM_ARGOCD_ADMIN" --arg project "$PROJECT" \
      '{Name: $name, Value: ., Type: "SecureString", KeyId: "alias/aws/ssm", Tags: [{Key: "project", Value: $project}]}' |
    aws_ ssm put-parameter --cli-input-json file:///dev/stdin >/dev/null
  log "generated the Argo CD admin password in $SSM_ARGOCD_ADMIN"
}

# Helm values with the bcrypt hash only; the password goes from SSM to htpasswd through a pipe (as in k3d-up.sh).
argocd_admin_values() {
  local hash
  # shellcheck disable=SC2016 # $2y$ and $2a$ are literal bcrypt prefixes
  hash="$(aws_ ssm get-parameter --name "$SSM_ARGOCD_ADMIN" --with-decryption --query Parameter.Value --output text |
    htpasswd -niBC 10 admin | cut -d: -f2- | tr -d '\n' | sed 's/^\$2y\$/$2a$/')"
  printf 'configs:\n  secret:\n    argocdServerAdminPassword: "%s"\n    argocdServerAdminPasswordMtime: "%s"\n' \
    "$hash" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

install_argocd() {
  [ -f "$ARGOCD_CHART_FILE" ] || die "missing $ARGOCD_CHART_FILE (sf-platform)"
  local values=(--values "$ARGOCD_VALUES")
  if [ -f "$ARGOCD_AWS_VALUES" ]; then
    values+=(--values "$ARGOCD_AWS_VALUES")
  elif dry_run; then
    warn "missing $ARGOCD_AWS_VALUES (sf-platform: aws values without the SOPS/age mount)"
  else
    die "missing $ARGOCD_AWS_VALUES (sf-platform: aws values without the SOPS/age mount)"
  fi
  local chart=(argocd "$(yq '.chart' "$ARGOCD_CHART_FILE")" --repo "$(yq '.repo' "$ARGOCD_CHART_FILE")"
    --version "$(yq '.version' "$ARGOCD_CHART_FILE")" --kubeconfig "$KUBECONFIG_FILE" --namespace argocd
    --create-namespace --wait --timeout 10m)
  ensure_argocd_admin_password
  if dry_run; then
    run helm upgrade --install "${chart[@]}" "${values[@]}" --values "<bcrypt of $SSM_ARGOCD_ADMIN>"
    return 0
  fi
  local admin_values
  admin_values="$(argocd_admin_values)"
  # shellcheck disable=SC2016 # literal bcrypt prefix
  case "$admin_values" in *'"$2a$'*) ;; *) die "could not derive the Argo CD admin password hash" ;; esac
  helm upgrade --install "${chart[@]}" "${values[@]}" --values <(printf '%s\n' "$admin_values") >&2
}

# Root apps are created by the same sf-platform mechanism as `make up`; cloud-up only passes the
# aws overlay and the session parameters (the full list is in docs/runbooks/cloud-session.md).
# Root apps come from the same sf-platform mechanism as `make up` (scripts/platform-root-apps.sh, ADR 0206);
# cloud-up only passes the aws overlay and the session parameters. ROOT_APP_ARGS is set by root_app_args.
root_app_args() {
  local vpc_id
  # Layer 1 is permanent, so the VPC is known before layer 2 exists.
  vpc_id="$(aws_ ec2 describe-vpcs --filters "Name=tag:Name,Values=$PROJECT" "Name=tag:project,Values=$PROJECT" \
    --query 'Vpcs[0].VpcId' --output text)" || die "cannot read the shopflow VPC"
  case "$vpc_id" in vpc-*) ;; *) die "no shopflow VPC (is layer 1 applied?)" ;; esac
  ROOT_APP_ARGS=(
    --overlay aws --revision "$REVISION" --profiles "$PROFILES"
    --param "operatorCidr=$OPERATOR_CIDR"
    --param "pg.recoveryFrom=$PG_RECOVERY_FROM"
    --param "pg.serverName=$PG_SERVER_NAME"
    --param "pg.recoveryTargetTime=$PITR"
    --param "cdcEpoch=$CDC_EPOCH"
    --param "aws.region=$REGION"
    --param "aws.accountId=$(account_id)"
    --param "aws.vpcId=$vpc_id"
    --param "aws.dataBucket=$(data_bucket)"
    --param "aws.clusterName=$CLUSTER"
  )
}

# Preflight: profiles and parameters are validated by the hook itself (no cluster access) before anything bills.
check_root_apps() {
  [ -x "$ROOT_APPS_HOOK" ] || die "missing $ROOT_APPS_HOOK (sf-platform root app mechanism)"
  root_app_args
  "$ROOT_APPS_HOOK" "${ROOT_APP_ARGS[@]}" --check || die "root apps rejected the session parameters or profiles"
  log "root apps: profiles $PROFILES and session parameters accepted"
}

create_root_apps() {
  if dry_run; then
    log "dry-run: root Applications that would be applied:"
    "$ROOT_APPS_HOOK" "${ROOT_APP_ARGS[@]}" --print >&2
    return 0
  fi
  env KUBECONFIG="$KUBECONFIG_FILE" "$ROOT_APPS_HOOK" "${ROOT_APP_ARGS[@]}"
}

apps_healthy() {
  kube -n argocd get applications.argoproj.io -o json 2>/dev/null |
    jq -e '(.items | length) > 0 and all(.items[]; .status.sync.status == "Synced" and .status.health.status == "Healthy")' >/dev/null
}

# ---- step 6: Postgres chain and CDC epoch --------------------------------------------------------

cnpg_ready() {
  kube -n shop get clusters.postgresql.cnpg.io shop-db -o json 2>/dev/null |
    jq -e 'any(.status.conditions[]?; .type == "Ready" and .status == "True")' >/dev/null
}

postgres_chain() {
  wait_until 1800 "CNPG cluster shop-db Ready" cnpg_ready || die "shop-db did not become Ready"
  if [ "$PG_MODE" = initdb ]; then
    ssm_put_new "$SSM_PG_MARKER" "$SESSION_ID"
  fi
  # Every session starts a new serverName; a fresh base backup makes it the head of the chain
  # before anything else depends on it.
  take_backup "shop-db-$SESSION_ID-initial"
}

cdc_epoch_snapshot() {
  if [ -x "$CDC_EPOCH_HOOK" ]; then
    # Idempotent (sf-data): creates the control topic, records meta.cdc_epochs, waits for SnapshotCompleted.
    run env KUBECONFIG="$KUBECONFIG_FILE" "$CDC_EPOCH_HOOK" wait --epoch "$CDC_EPOCH"
  else
    warn "missing $CDC_EPOCH_HOOK (sf-data): epoch $CDC_EPOCH is not recorded in meta.cdc_epochs"
  fi
}

# ---- step 7: smoke ---------------------------------------------------------------------------------

gateway_host() {
  kube -n envoy-gateway-system get svc -l gateway.envoyproxy.io/owning-gateway-name -o json 2>/dev/null |
    jq -r '[.items[].status.loadBalancer.ingress[]?.hostname | select(. != null)] | first // empty'
}

gateway_answers() {
  local host
  host="$(gateway_host)"
  [ -n "$host" ] && curl -fsSk --max-time 10 -o /dev/null "https://$host/products"
}

# Count the smoke order in bronze for this session's epoch (bronze is append-only and keeps older
# epochs). Trino requires HTTPS + password; the password goes to the CLI through stdin, never argv.
# shellcheck disable=SC2016 # $1..$3 in trino_cli are expanded by sh inside the pod
order_in_bronze() {
  local query count trino_cli
  query="SELECT count(*) FROM $BRONZE_ORDERS_TABLE WHERE id = $1 AND _cdc_epoch = $CDC_EPOCH"
  trino_cli='read -r TRINO_PASSWORD && export TRINO_PASSWORD && exec trino --server https://localhost:8443'
  trino_cli="$trino_cli"' --truststore-path /etc/trino/tls/ca.crt --truststore-type PEM --user "$1" --password'
  trino_cli="$trino_cli"' --catalog "$2" --output-format TSV --execute "$3"'
  count="$(kube -n lakehouse get secret "$TRINO_PASSWORD_SECRET" -o json | jq -r '.data.password | @base64d' |
    kube -n lakehouse exec -i deploy/trino-coordinator -- sh -c "$trino_cli" smoke "$TRINO_USER" "$TRINO_CATALOG" "$query" 2>/dev/null |
    tr -d '[:space:]')"
  [ "${count:-0}" -ge 1 ] 2>/dev/null
}

smoke_test() {
  if dry_run; then
    log "dry-run: would place one order through the NLB and wait for it in $TRINO_CATALOG.$BRONZE_ORDERS_TABLE (epoch $CDC_EPOCH)"
    return 0
  fi
  wait_until 900 "the NLB answers GET /products" gateway_answers || die "the shop is not reachable through the NLB"
  local host product order
  host="$(gateway_host)"
  product="$(curl -fsSk --max-time 10 "https://$host/products" | jq -er '.[0].id')" || die "no products: is the database seeded?"
  order="$(curl -fsSk --max-time 10 -X POST -H 'content-type: application/json' \
    --data "{\"customer_id\":$SMOKE_CUSTOMER_ID,\"items\":[{\"product_id\":$product,\"quantity\":1}]}" \
    "https://$host/checkout" | jq -er '.id')" || die "checkout through the NLB failed"
  log "order $order placed through https://$host"
  wait_until 120 "order $order in $TRINO_CATALOG.$BRONZE_ORDERS_TABLE (epoch $CDC_EPOCH)" order_in_bronze "$order" ||
    die "order $order did not reach bronze within 2 minutes"
}

# ---- step 9–10: lease, kill switches ---------------------------------------------------------------

arm_kill_switches() {
  write_lease
  local lease grace
  lease="$(epoch_from_iso "$LEASE_ISO")"
  grace="$(contract .reaper.grace_hours)"
  schedule_upsert "$(iso_from_epoch $((lease + grace * 3600)))"
  log "backup reaper (Lambda) scheduled from lease + ${grace}h"
  # GitHub disables cron workflows after 60 days without activity; re-enable on every session.
  run gh workflow enable cloud-reaper.yml --repo "$(contract .github_repository)" ||
    warn "could not enable the GitHub reaper; the Lambda reaper still covers this session"
}

on_exit() {
  local status=$?
  if [ "$status" -eq 0 ] || [ -z "${SESSION_ID:-}" ] || dry_run; then return 0; fi
  warn "cloud-up stopped. The lease still bounds the cost (the reapers act at expiry)."
  warn "Fix the cause, then: scripts/cloud-up.sh --resume   (or scripts/cloud-down.sh to give up)"
}

main() {
  parse_args "$@"
  require_cmds aws tofu kubectl helm jq yq curl gh openssl htpasswd
  trap on_exit EXIT
  dry_run && log "DRY-RUN: reads only; changes are printed"

  step 1 "preflight"
  require_role "$OPERATOR_ROLE"
  check_budget_action
  check_kubernetes_version
  if [ "$RESUME" = 1 ]; then resume_session; else new_session; fi
  check_root_apps
  mkdir -p "$OUT_DIR/$SESSION_ID"

  step 2 "apply layer 2 (EKS, nodes, addons, Pod Identity)"
  local t_apply t_nodes t_smoke
  t_apply="$(now_epoch)"
  apply_cluster_layer

  step 3 "kubeconfig and nodes"
  if dry_run && [ "$(cluster_status)" = ABSENT ]; then
    log "dry-run: no cluster yet; later steps are printed from the plan only"
  else
    update_kubeconfig
  fi
  wait_until 1200 "a Ready node" nodes_ready || die "no node became Ready"
  t_nodes="$(now_epoch)"

  step 4 "Argo CD and root apps (overlay aws)"
  install_argocd
  create_root_apps

  step 5 "wait for every Application to be Synced and Healthy"
  wait_until 2700 "all Argo CD Applications Synced/Healthy" apps_healthy || die "Applications did not converge"

  step 6 "Postgres backup chain and CDC epoch"
  postgres_chain
  cdc_epoch_snapshot

  step 7 "smoke: checkout through the NLB reaches bronze"
  smoke_test
  t_smoke="$(now_epoch)"

  step 8 "RTO"
  local rto_infra=$((t_nodes - t_apply)) rto_service=$((t_smoke - t_nodes))
  jq -n --arg session "$SESSION_ID" --argjson infra "$rto_infra" --argjson service "$rto_service" --arg pg "$PG_MODE" \
    '{session: $session, rto_infra_seconds: $infra, rto_service_seconds: $service, postgres: $pg}' >"$OUT_DIR/$SESSION_ID/timings.json"
  log "RTO-infra (apply -> node Ready): ${rto_infra}s; RTO-service (node Ready -> smoke pass): ${rto_service}s"

  step 9 "lease and kill switches"
  arm_kill_switches
  log "session $SESSION_ID is up until $LEASE_ISO. Extend: make cloud-extend; stop: make cloud-down"
}

main "$@"
