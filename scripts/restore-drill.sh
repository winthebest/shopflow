#!/usr/bin/env bash
# Local restore drill on k3d (docs/runbooks/restore.md; profiles core + drill, contract: docs/contracts/gitops.md).
# The database is destroyed while k6 writes, without a final backup, then recovered from its barman-cloud chain in
# SeaweedFS. Never touches AWS.
#
#   restore-drill.sh prepare   base backup on shop-db's current chain (`make up PROFILES=core,drill` already archives)
#   restore-drill.sh run       k6 writes -> shop-db is destroyed -> recovery from the chain (latest, or --pitr N:
#                              N seconds before the disaster) -> RPO, acked orders lost, RTO
#
# Options: --cluster NAME (sf-main)  --profiles LIST (core,drill)  --revision REV (main)  --warmup SECONDS (120)
#          --rate CHECKOUTS_PER_S (20)  --pitr SECONDS  --dry-run
# Environment: BASE_URL (https://shop.127.0.0.1.sslip.io:<the cluster's HTTPS port>).
# Output: out/drill/run-<stamp>/{acks.jsonl,k6.log,survivors.txt,result.json}; the head of the chain in
# out/drill/pointer.json.
#
# RPO uses the k6 clock only (acked_at): last ack before the disaster minus the last ack whose order survived.
set -euo pipefail

CLOUD_OUT_DIR="${CLOUD_OUT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/out/drill}"
export CLOUD_OUT_DIR
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

# AWS is never part of a local drill.
aws_() { die "restore-drill never calls AWS"; }

ROOT_APPS_HOOK="${ROOT_APPS_HOOK:-$REPO_ROOT/scripts/platform-root-apps.sh}"
K6_SCRIPT="$REPO_ROOT/loadtest/checkout.js"
POINTER_FILE="$OUT_DIR/pointer.json"
RECOVERY_TIMEOUT="${DRILL_RECOVERY_TIMEOUT:-1800}"
PITR_TOLERANCE=2 # seconds around the PITR mark where an order may land on either side

K3D_CLUSTER=sf-main
PROFILES=core,drill
REVISION=main
WARMUP=120
RATE=20
PITR=""
COMMAND=""

ARGO_PAUSED=0
K6_PID=""

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

parse_args() {
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  COMMAND="${1:-}"
  [ $# -gt 0 ] && shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --cluster) K3D_CLUSTER="${2:?--cluster needs a value}"; shift ;;
      --profiles) PROFILES="${2:?--profiles needs a value}"; shift ;;
      --revision) REVISION="${2:?--revision needs a value}"; shift ;;
      --warmup) WARMUP="${2:?--warmup needs a value}"; shift ;;
      --rate) RATE="${2:?--rate needs a value}"; shift ;;
      --pitr) PITR="${2:?--pitr needs a value}"; shift ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  case "$COMMAND" in prepare | run) ;; *) usage >&2; die "usage: $0 prepare|run [options]" ;; esac
  case ",$PROFILES," in *,drill,*) ;; *) die "--profiles must include drill (seaweedfs + the barman-cloud plugin)" ;; esac
}

# HTTPS load-balancer port of each cluster (docs/contracts/environment.md).
base_url() {
  local port
  case "$K3D_CLUSTER" in
    sf-main) port=9443 ;;
    sf-platform) port=8443 ;;
    sf-sre) port=8444 ;;
    sf-data) port=8445 ;;
    sf-app) port=8446 ;;
    *) port=443 ;;
  esac
  printf '%s' "${BASE_URL:-https://shop.127.0.0.1.sslip.io:$port}"
}

# A kubeconfig with only this cluster's context, so every kubectl call and the root-app hook hit the same cluster.
KUBECONFIG_FILE="$OUT_DIR/kubeconfig-$$"
write_kubeconfig() {
  mkdir -p "$OUT_DIR"
  (umask 077 && kubectl config view --minify --flatten --context "k3d-$K3D_CLUSTER" >"$KUBECONFIG_FILE") ||
    die "no kubectl context k3d-$K3D_CLUSTER: is the cluster up (make up CLUSTER=$K3D_CLUSTER)?"
}

save_pg_pointer() {
  local pointer
  pointer="$(jq -c --arg backup "$2" '. + {backup: $backup}' <<<"$1")"
  if dry_run; then
    log "DRY-RUN: would record the chain head $pointer in $POINTER_FILE"
    return 0
  fi
  printf '%s\n' "$pointer" >"$POINTER_FILE"
}

root_apps() {
  run env KUBECONFIG="$KUBECONFIG_FILE" KUBE_CONTEXT="k3d-$K3D_CLUSTER" "$ROOT_APPS_HOOK" \
    --overlay local --revision "$REVISION" --profiles "$PROFILES" "$@" >&2
}

# One stamp per invocation names the new backup chain and the run directory (DRILL_STAMP pins it, e.g. in tests).
STAMP="${DRILL_STAMP:-$(date -u +%Y%m%dt%H%M%Sz)}"
new_server_name() { printf 'shop-db-drill-%s' "$STAMP"; }

# ---- checks ------------------------------------------------------------------------------------

apps_ready() {
  kube -n argocd get applications.argoproj.io -o json 2>/dev/null | jq -e '
    [.items[] | select(.metadata.name == ("seaweedfs", "cnpg-barman-plugin", "shop-db"))
      | select(.status.sync.status == "Synced" and .status.health.status == "Healthy")] | length == 3' >/dev/null
}

cnpg_ready() {
  kube -n shop get clusters.postgresql.cnpg.io shop-db -o json 2>/dev/null |
    jq -e 'any(.status.conditions[]?; .type == "Ready" and .status == "True")' >/dev/null
}

archiving_to() {
  kube -n shop get clusters.postgresql.cnpg.io shop-db -o json 2>/dev/null | jq -e --arg server "$1" '
    any(.spec.plugins[]?; .parameters.serverName == $server)
    and any(.status.conditions[]?; .type == "ContinuousArchiving" and .status == "True")' >/dev/null
}

db_gone() {
  [ -z "$(kube -n shop get pods,pvc -l cnpg.io/cluster=shop-db -o name 2>/dev/null)" ] &&
    ! kube -n shop get clusters.postgresql.cnpg.io shop-db >/dev/null 2>&1
}

preflight() {
  require_cmds kubectl jq curl k6
  write_kubeconfig
  apps_ready || die "profile drill is not Synced/Healthy (seaweedfs, cnpg-barman-plugin, shop-db): make up PROFILES=$PROFILES"
  cnpg_ready || die "CNPG cluster shop-db is not Ready"
}

# ---- prepare -----------------------------------------------------------------------------------

# The chain shop-db archives to (pg.serverName; make up sets it whenever the barman-cloud plugin is in the profiles).
current_chain() {
  kube -n shop get clusters.postgresql.cnpg.io shop-db -o json |
    jq -r '[.spec.plugins[]? | select(.name == "barman-cloud.cloudnative-pg.io") | .parameters.serverName] | first // empty'
}

prepare() {
  local server
  step 1 "preflight"
  preflight
  server="$(current_chain)"
  [ -n "$server" ] || die "shop-db has no backup chain: make up PROFILES=$PROFILES sets pg.serverName with the barman-cloud plugin"
  archiving_to "$server" || die "WAL archiving to $server is not healthy (Cluster condition ContinuousArchiving)"
  step 2 "base backup on the chain $server (SeaweedFS bucket pg-backup)"
  take_backup "$server-base-$STAMP"
  log "ready: scripts/restore-drill.sh run --cluster $K3D_CLUSTER"
}

# ---- run ---------------------------------------------------------------------------------------

argo_controller() {
  run kube -n argocd scale statefulset argocd-application-controller --replicas="$1" >/dev/null
}

controller_stopped() {
  [ -z "$(kube -n argocd get pods -l app.kubernetes.io/name=argocd-application-controller -o name 2>/dev/null)" ]
}

start_k6() {
  local dir="$1" url="$2"
  if dry_run; then
    log "DRY-RUN: would run k6 at $RATE checkouts/s against $url, acks in $dir/acks.jsonl"
    return 0
  fi
  k6 run --quiet --insecure-skip-tls-verify --log-format raw --console-output "$dir/acks.jsonl" \
    -e BASE_URL="$url" -e RATE="$RATE" -e DURATION=60m "$K6_SCRIPT" >"$dir/k6.log" 2>&1 &
  K6_PID=$!
}

stop_k6() {
  [ -n "$K6_PID" ] || return 0
  # TERM, not INT: a background job of a non-interactive shell starts with SIGINT ignored. k6 stops on both.
  kill -TERM "$K6_PID" 2>/dev/null || true
  wait "$K6_PID" 2>/dev/null || true
  K6_PID=""
}

ack_count() {
  local n
  n="$(grep -c '"ack"' "$1" 2>/dev/null || true)"
  printf '%s' "${n:-0}"
}

# Order ids present after the recovery, read before anything can write again (k6 is stopped).
survivors() {
  local pod
  pod="$(kube -n shop get pods -l cnpg.io/cluster=shop-db,cnpg.io/instanceRole=primary -o name | head -n 1)"
  [ -n "$pod" ] || die "no shop-db primary after the recovery"
  kube -n shop exec "$pod" -c postgres -- psql -d shop -At -c "SELECT id FROM orders ORDER BY id"
}

checkout_answers() {
  local url="$1" product
  product="$(curl -fsSk --max-time 10 "$url/products" | jq -er '.[0].id')" || return 1
  curl -fsSk --max-time 10 -X POST -H 'content-type: application/json' \
    --data "{\"customer_id\":1,\"items\":[{\"product_id\":$product,\"quantity\":1}]}" "$url/checkout" |
    jq -e '.id' >/dev/null
}

# acks.jsonl + survivors.txt -> result JSON. Timestamps are k6's (acked_at), so no clock is compared with another.
metrics() {
  local acks="$1" survivors="$2" disaster="$3" mark="${4:-}"
  jq -Rn --rawfile ids "$survivors" --argjson disaster "$disaster" --arg mark "$mark" --argjson tolerance "$PITR_TOLERANCE" '
    def ts: (.[0:19] + "Z" | fromdateiso8601) + ((.[20:23] | tonumber? // 0) / 1000);
    ($ids | split("\n") | map(select(length > 0)) | map({key: ., value: true}) | from_entries) as $alive
    | [inputs | fromjson? | .ack? // empty | {id: (.order_id | tostring), t: (.acked_at | ts)} | select(.t < $disaster)] as $acked
    | [$acked[] | select($alive[.id])] as $kept
    | ($acked | map(.t) | max) as $last
    | ($kept | map(.t) | max) as $last_kept
    | {
        acked: ($acked | length),
        lost: ([$acked[] | select($alive[.id] | not)] | length),
        rpo_seconds: (if ($kept | length) == ($acked | length) then 0
                      elif $last_kept == null then null
                      else (($last - $last_kept) * 1000 | round) / 1000 end)
      }
    + (if $mark == "" then {} else ($mark | tonumber) as $m | {
        pitr_mark_epoch: $m,
        pitr_before_mark_lost: ([$acked[] | select(.t <= $m - $tolerance and ($alive[.id] | not))] | length),
        pitr_after_mark_kept: ([$acked[] | select(.t > $m + $tolerance and $alive[.id])] | length)
      } end)' "$acks"
}

drill() {
  local old_server new_server url dir disaster mark="" mark_iso="" db_back service_back pitr_args=()
  step 1 "preflight"
  [ -f "$POINTER_FILE" ] || die "no backup chain yet: run scripts/restore-drill.sh prepare first"
  old_server="$(jq -er .serverName "$POINTER_FILE")" || die "unreadable $POINTER_FILE"
  preflight
  backup_completed "$(jq -er .backup "$POINTER_FILE")" || die "the base backup of $old_server is not completed"
  archiving_to "$old_server" || die "shop-db does not archive to $old_server: run prepare again"
  url="$(base_url)"
  dir="$OUT_DIR/run-$STAMP"
  mkdir -p "$dir"

  step 2 "k6 writes ($RATE checkouts/s) for ${WARMUP}s"
  start_k6 "$dir" "$url"
  dry_run || sleep "$WARMUP"
  if ! dry_run && [ "$(ack_count "$dir/acks.jsonl")" -eq 0 ]; then
    die "no acked checkout after ${WARMUP}s: is $url reachable (see $dir/k6.log)?"
  fi
  if [ -n "$PITR" ]; then
    mark=$(($(now_epoch) - PITR))
    mark_iso="$(iso_from_epoch "$mark")"
    pitr_args=(--param "pg.recoveryTargetTime=$mark_iso")
    log "PITR mark: $mark_iso (${PITR}s before the disaster)"
  fi

  step 3 "disaster: shop-db is destroyed without a final backup"
  disaster="$(now_epoch)"
  ARGO_PAUSED=1 # Argo CD would recreate the Cluster with initdb and the old chain name before the recovery params land
  argo_controller 0
  wait_until 120 "the Argo CD application controller stopped" controller_stopped || die "the Argo CD controller did not stop"
  run kube -n shop delete pod -l cnpg.io/cluster=shop-db --grace-period=0 --force --wait=false >&2
  run kube -n shop delete clusters.postgresql.cnpg.io shop-db --wait=false >&2
  run kube -n shop delete pvc -l cnpg.io/cluster=shop-db --wait=false >&2
  sleep "${DRILL_K6_DRAIN:-5}"
  stop_k6
  wait_until 300 "shop-db pods, volumes and Cluster gone" db_gone || die "shop-db was not fully removed"

  step 4 "recovery from $old_server${mark_iso:+ to $mark_iso}"
  new_server="$(new_server_name)"
  root_apps --param "pg.serverName=$new_server" --param "pg.recoveryFrom=$old_server" ${pitr_args[@]+"${pitr_args[@]}"}
  argo_controller 1
  ARGO_PAUSED=0
  wait_until "$RECOVERY_TIMEOUT" "shop-db recovered and Ready" cnpg_ready || die "shop-db did not recover"
  db_back="$(now_epoch)"

  if dry_run; then
    log "dry-run: would read the surviving order ids, place one checkout, compute RPO/RTO and take a base backup"
    return 0
  fi
  step 5 "measure"
  survivors >"$dir/survivors.txt"
  wait_until 600 "a checkout through $url succeeds" checkout_answers "$url" || die "the shop did not come back"
  service_back="$(now_epoch)"
  metrics "$dir/acks.jsonl" "$dir/survivors.txt" "$disaster" "$mark" |
    jq --arg from "$old_server" --arg to "$new_server" --arg mark "$mark_iso" \
      --argjson db "$((db_back - disaster))" --argjson service "$((service_back - disaster))" \
      '. + {recovered_from: $from, new_chain: $to, pitr_mark: (if $mark == "" then null else $mark end),
            rto_db_seconds: $db, rto_service_seconds: $service}' >"$dir/result.json"
  log "result: $(jq -c . "$dir/result.json")"

  step 6 "base backup of the new chain $new_server"
  wait_until 600 "shop-db archives WAL to $new_server" archiving_to "$new_server" || die "WAL archiving to $new_server did not start"
  take_backup "$new_server-base"
  log "drill done: $dir/result.json (record it in docs/runbooks/restore.md)"
}

on_exit() {
  local status=$?
  stop_k6
  if [ "$ARGO_PAUSED" = 1 ]; then
    warn "restarting the Argo CD application controller (the drill stopped while it was paused)"
    kube -n argocd scale statefulset argocd-application-controller --replicas=1 >/dev/null 2>&1 || warn "scale it back by hand"
  fi
  rm -f "$KUBECONFIG_FILE"
  return "$status"
}

main() {
  parse_args "$@"
  trap on_exit EXIT
  dry_run && log "DRY-RUN: reads only; changes are printed"
  case "$COMMAND" in
    prepare) prepare ;;
    run) drill ;;
  esac
}

main "$@"
