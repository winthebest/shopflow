# shellcheck shell=bash
# Read-only Prometheus access for the sre-* scripts, sourced (not run). Needs CTX (kube context). prom_connect opens
# `kubectl port-forward svc/kps-prometheus` on a random local port and kills it when the script exits: unlike the API
# server's service proxy, it is not blocked by the observability NetworkPolicies.

kc() { kubectl --context "$CTX" "$@"; }
enc() { jq -rn --arg v "$1" '$v | @uri'; }
PF_PORT=""
prom_raw() { curl -fsS --max-time 60 "http://127.0.0.1:${PF_PORT}/api/v1/$1"; }
# prom '<promql>' → instant-query result array (JSON), [] on error
prom() { prom_raw "query?query=$(enc "$1")" 2>/dev/null | jq -c '.data.result // []' 2>/dev/null || echo '[]'; }
# scalar '<promql>' → first sample value, empty when none
scalar() { prom "$1" | jq -r '.[0].value[1] // empty'; }
count() { prom "$1" | jq 'length'; }
# prom_range '<promql>' START END STEP → range-query result array (JSON), [] on error
prom_range() {
  prom_raw "query_range?query=$(enc "$1")&start=$(enc "$2")&end=$(enc "$3")&step=$4" 2>/dev/null \
    | jq -c '.data.result // []' 2>/dev/null || echo '[]'
}

# Exits 2 when the cluster or Prometheus cannot be reached.
prom_connect() {
  kc get --raw /readyz >/dev/null 2>&1 || { echo "cannot reach the API server of context $CTX" >&2; exit 2; }
  PF_LOG="$(mktemp)"
  kc -n observability port-forward svc/kps-prometheus :9090 >"$PF_LOG" 2>&1 &
  PF_PID=$!
  trap 'kill "$PF_PID" 2>/dev/null; rm -f "$PF_LOG"' EXIT
  for _ in $(seq 1 50); do
    PF_PORT="$(grep -oE '127\.0\.0\.1:[0-9]+' "$PF_LOG" | head -1 | cut -d: -f2)"
    [[ -n "$PF_PORT" ]] && break
    sleep 0.2
  done
  if [[ -z "$PF_PORT" ]] || ! prom_raw "status/buildinfo" >/dev/null 2>&1; then
    echo "Prometheus (observability/kps-prometheus) not reachable through port-forward" >&2
    exit 2
  fi
}
