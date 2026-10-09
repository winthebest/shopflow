#!/usr/bin/env bash
# Collect a session's evidence before teardown and upload it to s3://<data>/evidence/<session>/:
# cluster state, Argo CD status, cloud-up timings (RTO), k6 summaries, and a Prometheus TSDB
# snapshot taken through the admin API (enabled only in the aws overlay; never exposed).
# Each part is best-effort so a missing component does not block the teardown; the upload is not.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/export-evidence.sh --session ID [--dry-run]

Environment: K6_SUMMARY_DIR (default out/k6), PROM_NAMESPACE (default observability),
PROM_SELECTOR (default app.kubernetes.io/name=prometheus), CLOUD_KUBECONFIG.
EOF
}

K6_SUMMARY_DIR="${K6_SUMMARY_DIR:-$REPO_ROOT/out/k6}"
PROM_NAMESPACE="${PROM_NAMESPACE:-observability}"
PROM_SELECTOR="${PROM_SELECTOR:-app.kubernetes.io/name=prometheus}"

capture() {
  local file="$1"
  shift
  if "$@" >"$file" 2>/dev/null; then log "captured $(basename "$file")"; else
    warn "could not capture $(basename "$file")"
    rm -f "$file"
  fi
}

prometheus_snapshot() {
  local dir="$1" pod name
  pod="$(kube -n "$PROM_NAMESPACE" get pods -l "$PROM_SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "$pod" ] || { warn "no Prometheus pod ($PROM_NAMESPACE, $PROM_SELECTOR); skipping TSDB snapshot"; return 0; }
  if dry_run; then
    run kube -n "$PROM_NAMESPACE" exec "$pod" -c prometheus -- wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot
    return 0
  fi
  name="$(kube -n "$PROM_NAMESPACE" exec "$pod" -c prometheus -- wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot | jq -er .data.name)" ||
    { warn "TSDB snapshot failed (admin API enabled in the aws overlay?)"; return 0; }
  kube -n "$PROM_NAMESPACE" exec "$pod" -c prometheus -- tar czf - -C /prometheus/snapshots "$name" >"$dir/prometheus-tsdb-$name.tgz" ||
    { warn "could not copy snapshot $name"; return 0; }
  # Free the pod's disk; the copy is local now.
  kube -n "$PROM_NAMESPACE" exec "$pod" -c prometheus -- rm -rf "/prometheus/snapshots/$name" || true
  log "captured Prometheus TSDB snapshot $name"
}

main() {
  local session=""
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  while [ $# -gt 0 ]; do
    case "$1" in
      --session) session="${2:?--session needs a value}"; shift ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  [ -n "$session" ] || { usage >&2; die "--session is required"; }
  require_cmds aws kubectl jq

  local dir="$OUT_DIR/$session/evidence"
  mkdir -p "$dir"
  capture "$dir/nodes.txt" kube get nodes -o wide
  capture "$dir/pods.txt" kube get pods --all-namespaces -o wide
  capture "$dir/argocd-applications.json" kube -n argocd get applications.argoproj.io -o json
  capture "$dir/events.txt" kube get events --all-namespaces --sort-by=.lastTimestamp
  capture "$dir/pvc.txt" kube get pvc --all-namespaces
  local file
  for file in timings.json netpol-probe.json; do
    [ ! -f "$OUT_DIR/$session/$file" ] || cp "$OUT_DIR/$session/$file" "$dir/"
  done
  if ls "$K6_SUMMARY_DIR"/*.json >/dev/null 2>&1; then
    mkdir -p "$dir/k6"
    cp "$K6_SUMMARY_DIR"/*.json "$dir/k6/"
    log "captured k6 summaries from $K6_SUMMARY_DIR"
  else
    warn "no k6 summaries in $K6_SUMMARY_DIR"
  fi
  prometheus_snapshot "$dir"

  run aws_ s3 cp --recursive --only-show-errors "$dir" "s3://$(data_bucket)/${EVIDENCE_PREFIX}$session/" ||
    die "upload to s3://$(data_bucket)/${EVIDENCE_PREFIX}$session/ failed"
  log "evidence -> s3://$(data_bucket)/${EVIDENCE_PREFIX}$session/ (local copy: $dir)"
}

main "$@"
