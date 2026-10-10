#!/usr/bin/env bash
# Gate check for the sf-sre components on a running cluster (read-only). Prometheus is queried through a
# `kubectl port-forward` on a random local port (killed on exit): unlike the API server's service proxy, it is not
# blocked by the observability NetworkPolicies. Prints PASS / WARN / FAIL / SKIP per check and exits non-zero on any
# FAIL.
#
# Run it while load is flowing (`make app-loadtest` in another terminal): without gateway traffic in the last 5
# minutes the checkout SLIs have no sample and their check fails.
#
# Usage: scripts/sre-gate-check.sh [--context CTX] [--window DURATION]
#   --context  kube context (default: $KUBE_CONTEXT, else k3d-sf-main)
#   --window   how far back restarts/peaks are checked (default 2h; use the time since the profiles came up)
#
# Checks:
#   pipeline   Prometheus targets up; OTLP span metrics; every rule group healthy
#   slo        every Sloth spec in slo/ has SLI samples (skipped while its profile is not up); CDC heartbeat and
#              WAL-retained series exist (profile data)
#   tempo      GOMEMLIMIT set; no restart / OOMKill in the window; peak working set vs the memory limit
#   alerts     no shopflow page firing; tickets listed; Alertmanager notification failures (placeholder webhook)
set -uo pipefail

CTX="${KUBE_CONTEXT:-k3d-sf-main}"
WINDOW="2h"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CTX="${2:?--context needs a value}"; shift ;;
    --window) WINDOW="${2:?--window needs a value}"; shift ;;
    *) echo "usage: $0 [--context CTX] [--window DURATION]" >&2; exit 2 ;;
  esac
  shift
done

FAILS=0
pass() { printf 'PASS  %-34s %s\n' "$1" "${2:-}"; }
warn() { printf 'WARN  %-34s %s\n' "$1" "${2:-}"; }
fail() { printf 'FAIL  %-34s %s\n' "$1" "${2:-}"; FAILS=$((FAILS + 1)); }
skip() { printf 'SKIP  %-34s %s\n' "$1" "${2:-}"; }

kc() { kubectl --context "$CTX" "$@"; }
enc() { jq -rn --arg v "$1" '$v | @uri'; }
PF_PORT=""
prom_raw() { curl -fsS --max-time 20 "http://127.0.0.1:${PF_PORT}/api/v1/$1"; }
# prom '<promql>' → instant-query result array (JSON), [] on error
prom() { prom_raw "query?query=$(enc "$1")" 2>/dev/null | jq -c '.data.result // []' 2>/dev/null || echo '[]'; }
# scalar '<promql>' → first sample value, empty when none
scalar() { prom "$1" | jq -r '.[0].value[1] // empty'; }
count() { prom "$1" | jq 'length'; }

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
echo "sre gate check — context $CTX, window $WINDOW"

# ---- pipeline -------------------------------------------------------------------------------------------------
down="$(prom 'up == 0' | jq -r '[.[] | (.metric.namespace // "") + "/" + (.metric.job // "") + "@" + (.metric.instance // "")] | join(" ")')"
if [[ -z "$down" ]]; then pass "targets up" "$(count 'up == 1') targets"; else fail "targets up" "down: $down"; fi

n="$(count 'up{job=~".*cnpg.*|.*shop-db.*"} == 1 or up{namespace="shop", pod=~"shop-db-.*"} == 1')"
if [[ "${n:-0}" -gt 0 ]]; then pass "CNPG scraped (PodMonitor cnpg-shop)" "$n target(s)"; else fail "CNPG scraped (PodMonitor cnpg-shop)" "no shop-db target up"; fi

n="$(count 'traces_span_metrics_calls_total{service_name="gateway", span_kind="SPAN_KIND_SERVER"}')"
if [[ "${n:-0}" -gt 0 ]]; then pass "OTLP span metrics (gateway)" "$n series"; else fail "OTLP span metrics (gateway)" "no series: is traffic flowing and otel-gateway up?"; fi

# Rule health. "unknown" = not evaluated yet (normal right after make up): WARN. Errors:
#   "duplicate sample for timestamp": the group evaluated a timestamp it had already written (Prometheus schedules
#     by wall clock, so a clock that steps back, as the Docker VM under k3d does, repeats one evaluation). The first
#     value is kept and only fast-changing recording rules notice: WARN, whatever the rule.
#   any other error: FAIL in shopflow rules, WARN in the chart's default rules (rule files observability-kps-*).
rules="$(prom_raw rules 2>/dev/null)"
rule_errors() {  # $1: jq filter on {dup, chart} selecting the errors to list
  jq -r "[.data.groups[] | (.file | split(\"/\") | last) as \$f | .name as \$g | .rules[] | select(.health == \"err\")
    | {r: (\$g + \"/\" + .name + \": \" + (.lastError // \"err\")), dup: ((.lastError // \"\") | test(\"duplicate sample for timestamp\")),
       chart: (\$f | startswith(\"observability-kps-\"))} | select($1) | .r] | join(\"; \")" <<<"$rules"
}
bad="$(rule_errors '(.dup or .chart) | not')"
soft="$(rule_errors '.dup or .chart')"
unknown="$(jq -r '[.data.groups[] | .rules[] | select(.health == "unknown")] | length' <<<"$rules")"
if [[ -n "$bad" ]]; then fail "rule groups healthy" "$bad"
elif [[ -n "$soft" ]]; then warn "rule groups healthy" "${soft:0:400}"
elif [[ "${unknown:-0}" -gt 0 ]]; then warn "rule groups healthy" "$unknown rule(s) not evaluated yet; re-run in a minute"
else pass "rule groups healthy"; fi

# ---- SLOs -----------------------------------------------------------------------------------------------------
# Some SLIs are not counted on purpose until their profile is up (same guards as their SLI rules in
# deploy/platform/slo/base/): no sample is then expected, so the check is skipped instead of failed.
data_up="$(count 'kube_namespace_status_phase{namespace="lakehouse", phase="Active"} == 1')"
data_age="$(scalar 'time() - max(kube_namespace_created{namespace="lakehouse"})')"
batch_age="$(scalar 'time() - max(kube_namespace_created{namespace="airflow"})')"
exporter_age="$(scalar 'time() - max(process_start_time_seconds{job="freshness-exporter"})')"
younger() { [[ -n "$1" ]] && awk -v a="$1" -v l="$2" 'BEGIN{exit !(a < l)}'; }  # age $1 (seconds) below $2
not_counted() {  # prints why service $1 has no SLI samples by design, nothing when samples are expected
  case "$1" in
    cdc)
      if [[ "${data_up:-0}" -eq 0 ]]; then echo "profile data not up (namespace lakehouse absent)"
      elif younger "$data_age" 900; then echo "profile data up for less than 15 minutes"
      elif younger "$exporter_age" 900; then echo "freshness exporter (re)started less than 15 minutes ago"; fi ;;
    gold)
      if [[ -z "$batch_age" ]]; then echo "profile batch not up (namespace airflow absent)"
      elif younger "$batch_age" 7200; then echo "profile batch up for less than 2h (first dbt run)"
      elif younger "$exporter_age" 900; then echo "freshness exporter (re)started less than 15 minutes ago"; fi ;;
  esac
}

# Every Sloth spec in slo/ must have SLI samples (one per SLO).
traffic="$(scalar 'sum(rate(traces_span_metrics_calls_total{service_name="gateway", span_kind="SPAN_KIND_SERVER"}[5m]))')"
for spec in "$ROOT"/slo/*.yaml; do
  svc="$(yq '.spec.service' "$spec")"; want="$(yq '.spec.slos | length' "$spec")"
  why="$(not_counted "$svc")"
  if [[ -n "$why" ]]; then skip "SLO $svc has SLI data" "$why"; continue; fi
  got="$(count "slo:sli_error:ratio_rate5m{sloth_service=\"$svc\"}")"
  if [[ "${got:-0}" -ge "$want" ]]; then pass "SLO $svc has SLI data" "$got/$want SLOs"
  elif [[ "$svc" == checkout ]] && awk -v t="${traffic:-0}" 'BEGIN{exit !(t == 0)}'; then
    fail "SLO $svc has SLI data" "$got/$want: no gateway traffic in the last 5 minutes; re-run while make app-loadtest runs"
  else fail "SLO $svc has SLI data" "$got/$want SLOs with a 5m sample (SLI source missing?)"; fi
done

if [[ "${data_up:-0}" -eq 0 ]]; then
  skip "CDC heartbeat and WAL-retained" "profile data not up (namespace lakehouse absent)"
else
  cdc_why="$(not_counted cdc)"
  n="$(count 'cdc:bronze_heartbeat_stale:minute')"
  if [[ -n "$cdc_why" ]]; then skip "CDC heartbeat staleness series" "$cdc_why"
  elif [[ "${n:-0}" -gt 0 ]]; then pass "CDC heartbeat staleness series"
  else fail "CDC heartbeat staleness series" "cdc:bronze_heartbeat_stale:minute missing"; fi

  bytes="$(scalar 'max(shopflow:pg_slot_wal_retained:bytes)')"; ratio="$(scalar 'max(shopflow:pg_slot_wal_retained:ratio)')"
  if [[ -n "$bytes" && -n "$ratio" ]]; then
    # restart_lsn moves by WAL segment (16 MiB), so the value normally swings between ~0 and ~16 MiB.
    pass "WAL-retained series (debezium_shop)" "$(awk -v b="$bytes" -v r="$ratio" 'BEGIN{printf "%.0f bytes (%.1f MiB), %.2f%% of max_slot_wal_keep_size", b, b/1048576, r*100}')"
  else
    fail "WAL-retained series (debezium_shop)" "missing (Debezium slot created? cnpg-shop scraped?)"
  fi
fi

# ---- Tempo (GOMEMLIMIT, profile obs) ----------------------------------------------------------------------------
if kc -n observability get pod tempo-0 >/dev/null 2>&1; then
  gml="$(kc -n observability get pod tempo-0 -o jsonpath='{.spec.containers[0].env[?(@.name=="GOMEMLIMIT")].value}')"
  if [[ -n "$gml" ]]; then pass "Tempo GOMEMLIMIT" "$gml"; else fail "Tempo GOMEMLIMIT" "not set on tempo-0"; fi
  r="$(scalar "sum(increase(kube_pod_container_status_restarts_total{namespace=\"observability\", pod=\"tempo-0\"}[$WINDOW]))")"
  oom="$(count 'kube_pod_container_status_last_terminated_reason{namespace="observability", pod="tempo-0", reason="OOMKilled"} == 1')"
  if [[ "${r%.*}" == "0" && "$oom" == 0 ]]; then pass "Tempo stable over $WINDOW" "0 restarts, no OOMKill"
  else fail "Tempo stable over $WINDOW" "restarts≈${r:-?}, last termination OOMKilled: $oom"; fi
  peak="$(scalar "max_over_time(container_memory_working_set_bytes{namespace=\"observability\", pod=\"tempo-0\", container=\"tempo\"}[$WINDOW])")"
  lim="$(scalar 'kube_pod_container_resource_limits{namespace="observability", pod="tempo-0", container="tempo", resource="memory"}')"
  if [[ -n "$peak" && -n "$lim" ]]; then
    pct="$(awk -v p="$peak" -v l="$lim" 'BEGIN{printf "%.0f", p/l*100}')"
    msg="$(awk -v p="$peak" -v l="$lim" 'BEGIN{printf "peak %.0f MiB of %.0f MiB", p/1048576, l/1048576}')"
    if [[ "$pct" -lt 90 ]]; then pass "Tempo peak memory" "$msg ($pct%)"; else warn "Tempo peak memory" "$msg ($pct%): close to the limit"; fi
  else
    warn "Tempo peak memory" "no cAdvisor/kube-state sample"
  fi
else
  skip "Tempo" "tempo-0 not present (profile obs-lite?)"
fi
r="$(scalar "sum(increase(kube_pod_container_status_restarts_total{namespace=\"observability\"}[$WINDOW]))")"
if [[ "${r%.*}" == "0" ]]; then pass "observability restarts over $WINDOW" "0"; else warn "observability restarts over $WINDOW" "≈${r:-?} (check kubectl -n observability get pods)"; fi

# ---- alerts ---------------------------------------------------------------------------------------------------
pages="$(prom 'ALERTS{alertstate="firing", severity="page"}' | jq -r '[.[] | .metric.alertname] | unique | join(" ")')"
if [[ -z "$pages" ]]; then pass "no page firing"; else fail "no page firing" "$pages"; fi
tickets="$(prom 'ALERTS{alertstate="firing", severity="ticket"}' | jq -r '[.[] | .metric.alertname] | unique | join(" ")')"
if [[ -z "$tickets" ]]; then pass "no ticket firing"; else warn "tickets firing" "$tickets"; fi
nf="$(scalar "sum(increase(alertmanager_notifications_failed_total[$WINDOW]))")"
if [[ -z "$nf" || "${nf%.*}" == "0" ]]; then pass "Alertmanager deliveries" "no failed notifications"
else warn "Alertmanager deliveries" "≈${nf%.*} failed in $WINDOW (webhook still the placeholder?)"; fi

echo "---"
if [[ "$FAILS" -eq 0 ]]; then echo "sre gate: PASS"; exit 0; fi
echo "sre gate: $FAILS FAIL(s)"
exit 1
