#!/usr/bin/env bash
# Export the numbers a game-day postmortem needs from Prometheus before the cluster goes away (read-only).
#   alerts.tsv        every alert that fired in the window: first and last firing time (UTC+7), severity
#   <name>.csv        one series per row of SERIES below, sampled every STEP: time (UTC+7), labels, value
# Time zone of the output: TZ (default Asia/Saigon). Times passed to --from/--to: RFC 3339 or Unix seconds.
#
# Usage: scripts/sre-gameday-evidence.sh --from TIME [--to TIME] [--context CTX] [--out DIR] [--step 60s]
#   --to defaults to now; --out defaults to out/gameday-<UTC timestamp>.
set -uo pipefail

CTX="${KUBE_CONTEXT:-k3d-sf-main}"
FROM="" TO="$(date -u +%s)" STEP="60s"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/out/gameday-$(date -u +%Y%m%dT%H%M%SZ)"
export TZ="${TZ:-Asia/Saigon}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CTX="${2:?--context needs a value}"; shift ;;
    --from) FROM="${2:?--from needs a value}"; shift ;;
    --to) TO="${2:?--to needs a value}"; shift ;;
    --out) OUT="${2:?--out needs a value}"; shift ;;
    --step) STEP="${2:?--step needs a value}"; shift ;;
    *) echo "usage: $0 --from TIME [--to TIME] [--context CTX] [--out DIR] [--step 60s]" >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$FROM" ]] || { echo "--from is required (RFC 3339 or Unix seconds)" >&2; exit 2; }

# name|PromQL. Series that do not exist on this cluster (profile not up) give an empty file.
SERIES=(
  "checkout-availability-error-5m|slo:sli_error:ratio_rate5m{sloth_service=\"checkout\", sloth_slo=\"availability\"}"
  "checkout-latency-error-5m|slo:sli_error:ratio_rate5m{sloth_service=\"checkout\", sloth_slo=\"latency\"}"
  "error-budget-remaining|slo:period_error_budget_remaining:ratio"
  "checkout-requests-per-s|sum(rate(traces_span_metrics_calls_total{service_name=\"gateway\", span_kind=\"SPAN_KIND_SERVER\", http_route=\"/checkout\"}[1m]))"
  "payments-attempts-per-s|sum by (attempt, outcome) (rate(orders_payments_attempts_total[1m]))"
  "payments-circuit-state|max by (instance) (orders_payments_circuit_state)"
  "cdc-stale-minute|cdc:bronze_heartbeat_stale:minute"
  "cdc-heartbeat-age-s|max(data_freshness_seconds{table=\"bronze.heartbeat\"})"
  "wal-retained-bytes|max by (slot_name) (shopflow:pg_slot_wal_retained:bytes)"
  "wal-retained-ratio|max by (slot_name) (shopflow:pg_slot_wal_retained:ratio)"
  "wal-growth-bytes-per-s|max by (slot_name) (deriv(shopflow:pg_slot_wal_retained:bytes[15m]))"
  "fulfillment-worker-replicas|max(kube_deployment_status_replicas{namespace=\"shop\", deployment=\"fulfillment-worker\"})"
)

# shellcheck source=scripts/sre-prom-lib.sh
. "$ROOT/scripts/sre-prom-lib.sh"
prom_connect
mkdir -p "$OUT"
echo "game-day evidence — context $CTX, $FROM → $TO, step $STEP, output $OUT"

# Alerts: ALERTS{alertstate="firing"} at 15s resolution, first and last firing sample per alert and severity.
prom_range 'ALERTS{alertstate="firing"}' "$FROM" "$TO" 15s | jq -r '
  ["first_firing", "last_firing", "alertname", "severity", "labels"],
  (sort_by(.values[0][0]) | .[] | [
    (.values[0][0] | strflocaltime("%Y-%m-%d %H:%M:%S")),
    (.values[-1][0] | strflocaltime("%Y-%m-%d %H:%M:%S")),
    .metric.alertname, (.metric.severity // ""),
    (.metric | del(.__name__, .alertname, .alertstate, .severity) | to_entries | map("\(.key)=\(.value)") | join(","))
  ]) | @tsv' > "$OUT/alerts.tsv"
column -t -s $'\t' "$OUT/alerts.tsv"

for row in "${SERIES[@]}"; do
  name="${row%%|*}"; query="${row#*|}"
  prom_range "$query" "$FROM" "$TO" "$STEP" | jq -r '
    ["time", "labels", "value"],
    (.[] | (.metric | to_entries | map("\(.key)=\(.value)") | join(",")) as $l
         | .values[] | [(.[0] | strflocaltime("%Y-%m-%d %H:%M:%S")), $l, .[1]]) | @csv' > "$OUT/$name.csv"
  printf '%-32s %s rows\n' "$name" "$(($(wc -l < "$OUT/$name.csv") - 1))"
done
