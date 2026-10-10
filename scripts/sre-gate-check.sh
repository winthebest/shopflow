#!/usr/bin/env bash
# Gate check for the sf-sre components on a running cluster (read-only; queries Prometheus and the API server
# through the kube-apiserver service proxy, so no port-forward). Prints PASS / WARN / FAIL / SKIP per check and
# exits non-zero on any FAIL.
#
# Usage: scripts/sre-gate-check.sh [--context CTX] [--window DURATION]
#   --context  kube context (default: $KUBE_CONTEXT, else k3d-sf-main)
#   --window   how far back restarts/peaks are checked (default 2h; use the time since the profiles came up)
#
# Checks:
#   pipeline   Prometheus targets up; OTLP span metrics; every rule group healthy
#   slo        checkout, cdc and (when defined) freshness SLOs have SLI samples; WAL-retained series exist
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
prom_raw() { kc get --raw "/api/v1/namespaces/observability/services/kps-prometheus:9090/proxy/api/v1/$1"; }
# prom '<promql>' → instant-query result array (JSON), [] on error
prom() { prom_raw "query?query=$(enc "$1")" 2>/dev/null | jq -c '.data.result // []' 2>/dev/null || echo '[]'; }
# scalar '<promql>' → first sample value, empty when none
scalar() { prom "$1" | jq -r '.[0].value[1] // empty'; }
count() { prom "$1" | jq 'length'; }

kc get --raw /readyz >/dev/null 2>&1 || { echo "cannot reach the API server of context $CTX" >&2; exit 2; }
prom_raw "status/buildinfo" >/dev/null 2>&1 || { echo "Prometheus (observability/kps-prometheus) not reachable" >&2; exit 2; }
echo "sre gate check — context $CTX, window $WINDOW"

# ---- pipeline -------------------------------------------------------------------------------------------------
down="$(prom 'up == 0' | jq -r '[.[] | (.metric.namespace // "") + "/" + (.metric.job // "") + "@" + (.metric.instance // "")] | join(" ")')"
if [[ -z "$down" ]]; then pass "targets up" "$(count 'up == 1') targets"; else fail "targets up" "down: $down"; fi

n="$(count 'up{job=~".*cnpg.*|.*shop-db.*"} == 1 or up{namespace="shop", pod=~"shop-db-.*"} == 1')"
if [[ "${n:-0}" -gt 0 ]]; then pass "CNPG scraped (PodMonitor cnpg-shop)" "$n target(s)"; else fail "CNPG scraped (PodMonitor cnpg-shop)" "no shop-db target up"; fi

n="$(count 'traces_span_metrics_calls_total{service_name="gateway", span_kind="SPAN_KIND_SERVER"}')"
if [[ "${n:-0}" -gt 0 ]]; then pass "OTLP span metrics (gateway)" "$n series"; else fail "OTLP span metrics (gateway)" "no series: is traffic flowing and otel-gateway up?"; fi

bad="$(prom_raw rules 2>/dev/null | jq -r '[.data.groups[] | .name as $g | .rules[] | select(.health != "ok") | $g + "/" + .name + ": " + (.lastError // .health)] | join("; ")')"
if [[ -z "$bad" ]]; then pass "rule groups healthy"; else fail "rule groups healthy" "$bad"; fi

# ---- SLOs -----------------------------------------------------------------------------------------------------
# Every Sloth spec in slo/ must have SLI samples (one per SLO); a service without a spec yet is skipped.
for spec in "$ROOT"/slo/*.yaml; do
  svc="$(yq '.spec.service' "$spec")"; want="$(yq '.spec.slos | length' "$spec")"
  got="$(count "slo:sli_error:ratio_rate5m{sloth_service=\"$svc\"}")"
  if [[ "$got" -ge "$want" ]]; then pass "SLO $svc has SLI data" "$got/$want SLOs"
  else fail "SLO $svc has SLI data" "$got/$want SLOs with a 5m sample (no traffic or SLI source missing?)"; fi
done
grep -qsl 'freshness' "$ROOT"/slo/*.yaml || skip "SLO gold freshness" "no spec in slo/ yet (sf-data)"

n="$(count 'cdc:bronze_heartbeat_stale:minute')"
if [[ "${n:-0}" -gt 0 ]]; then pass "CDC heartbeat staleness series"; else fail "CDC heartbeat staleness series" "cdc:bronze_heartbeat_stale:minute missing"; fi

bytes="$(scalar 'max(shopflow:pg_slot_wal_retained:bytes)')"; ratio="$(scalar 'max(shopflow:pg_slot_wal_retained:ratio)')"
if [[ -n "$bytes" && -n "$ratio" ]]; then
  pass "WAL-retained series (debezium_shop)" "$(awk -v b="$bytes" -v r="$ratio" 'BEGIN{printf "%.0f MiB, %.1f%% of max_slot_wal_keep_size", b/1048576, r*100}')"
else
  fail "WAL-retained series (debezium_shop)" "missing (data profile up? Debezium slot created? cnpg-shop scraped?)"
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
