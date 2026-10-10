#!/usr/bin/env bash
# Phase 4 acceptance on a running cluster with profile data (and obs/obs-lite): CDC end to end and the cdc-lag SLI.
#   1. the current CDC epoch's snapshot completed (scripts/cdc-epoch.sh wait);
#   2. snapshot: bronze.customers holds every Postgres customer in that epoch, as `_op = 'r'` (rows that existed when
#      Debezium took its snapshot) or `'c'` (rows inserted after it: on a fresh cluster the shop's seed can run after
#      the snapshot, seen on the batch slot of 2026-10-10);
#   3. one order inserted, updated and deleted in Postgres reaches bronze.orders within CDC_CHECK_TIMEOUT seconds
#      (default 120) as `c,u,d` in that epoch;
#   4. Prometheus has samples of the freshness probe and of the per-minute SLI cdc:bronze_heartbeat_stale:minute.
# Prints PASS/FAIL per check; exits 1 if any failed. Reads Postgres as shop_app and bronze through Trino as `exporter`
# (catalog lake_ro); passwords go to the pods on stdin, never on a command line. Prometheus is reached with
# `kubectl port-forward` on 127.0.0.1 and a free port kubectl picks (docs/adr/0205; unlike the API server's service
# proxy, NetworkPolicies do not apply), stopped on exit; same approach as scripts/sre-gate-check.sh.
# Usage: CLUSTER=sf-data scripts/data-cluster-check.sh   (KUBE_CONTEXT overrides the context k3d-<CLUSTER>)
set -euo pipefail

cd "$(dirname "$0")/.."

KUBE_CONTEXT="${KUBE_CONTEXT:-k3d-${CLUSTER:-sf-main}}"
TIMEOUT="${CDC_CHECK_TIMEOUT:-120}"
kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }
secret_value() { kc -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }

failed=0
result() {
  if [[ "$1" == PASS ]]; then echo "PASS $2"; else echo "FAIL $2" >&2; failed=1; fi
}

# psql as shop_app on the CNPG primary; SQL on stdin.
psql_shop() {
  local pod
  pod="$(kc -n shop get pod -l cnpg.io/cluster=shop-db,cnpg.io/instanceRole=primary -o name | head -1)"
  { secret_value shop shop-db-app password; echo; cat; } | kc -n shop exec -i "$pod" -c postgres -- sh -c \
    'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U shop_app -d shop -v ON_ERROR_STOP=1 -qAt'
}

# One Trino query as `exporter` on catalog lake_ro, TSV without header.
trino_ro() {
  local pod
  pod="$(kc -n lakehouse get pod -l app.kubernetes.io/name=trino,app.kubernetes.io/component=coordinator -o name \
    | head -1)"
  # shellcheck disable=SC2016 # expanded by the pod's shell: the password from stdin, the query as $1
  secret_value lakehouse trino-exporter password | kc -n lakehouse exec -i "$pod" -- sh -c \
    'IFS= read -r TRINO_PASSWORD; export TRINO_PASSWORD; exec trino --server https://localhost:8443 --insecure \
      --user exporter --password --catalog lake_ro --output-format TSV --execute "$1"' sh "$1"
}

# Prometheus through a port-forward on a free local port (started once, stopped on exit).
PROM_PORT="" PF_PID="" PF_LOG="$(mktemp)"
trap 'if [[ -n "$PF_PID" ]]; then kill "$PF_PID" 2> /dev/null || true; fi; rm -f "$PF_LOG"' EXIT
prom_forward() {
  # kubectl itself, not the kc function: `kc ... &` backgrounds a subshell, $! is that subshell, and killing it on
  # exit left the kubectl child running (one orphaned port-forward per run).
  kubectl --context "$KUBE_CONTEXT" -n observability port-forward --address 127.0.0.1 svc/kps-prometheus :9090 \
    > "$PF_LOG" 2>&1 &
  PF_PID=$!
  for _ in $(seq 1 20); do
    PROM_PORT="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) .*/\1/p' "$PF_LOG" | head -1)"
    [[ -n "$PROM_PORT" ]] && curl -fsS -o /dev/null "http://127.0.0.1:$PROM_PORT/-/ready" 2> /dev/null && return 0
    kill -0 "$PF_PID" 2> /dev/null || break
    sleep 1
  done
  echo "port-forward to Prometheus failed: $(tr '\n' ' ' < "$PF_LOG")" >&2
  return 1
}

# Number of series an instant PromQL query returns.
prom_series() {
  curl -fsS --get --data-urlencode "query=$1" "http://127.0.0.1:$PROM_PORT/api/v1/query" | jq '.data.result | length'
}

epoch="$(secret_value kafka cdc-epoch epoch)"
echo "== CDC epoch $epoch ($KUBE_CONTEXT)"

if KUBE_CONTEXT="$KUBE_CONTEXT" scripts/cdc-epoch.sh wait --epoch "$epoch" --timeout 900; then
  result PASS "snapshot of epoch $epoch completed (meta.cdc_epochs.snapshot_completed_at)"
else
  result FAIL "snapshot of epoch $epoch did not complete"
fi

customers="$(psql_shop <<< 'SELECT count(*) FROM customers')"
deadline=$((SECONDS + TIMEOUT))
snapshot=0 reads=0 inserts=0
until ((snapshot >= customers)) || ((SECONDS > deadline)); do
  # One TSV row: distinct customers as r or c, those read by the snapshot (r), those streamed as inserts (c).
  read -r snapshot reads inserts < <(trino_ro "SELECT count(DISTINCT id),
      count(DISTINCT id) FILTER (WHERE _op = 'r'), count(DISTINCT id) FILTER (WHERE _op = 'c')
    FROM bronze.customers WHERE _op IN ('r', 'c') AND _cdc_epoch = $epoch" || echo "0 0 0")
  ((snapshot >= customers)) || sleep 10
done
counts="$snapshot/$customers customers in epoch $epoch: $reads as r (snapshot) + $inserts as c (inserted after it)"
if ((snapshot >= customers)); then
  result PASS "snapshot in bronze: $counts"
else
  result FAIL "snapshot in bronze: $counts"
fi

order="$(psql_shop <<< "INSERT INTO orders (customer_id, status, total)
  SELECT min(id), 'pending', 1.00 FROM customers RETURNING id")"
# `failed`, not `paid`: the fulfillment worker (profile ops) ships paid orders, and its shipment row would then block
# the DELETE (foreign key shipments -> orders). Still an `u` event.
psql_shop > /dev/null <<< "UPDATE orders SET status = 'failed' WHERE id = $order; DELETE FROM orders WHERE id = $order"
started=$SECONDS
ops=""
until [[ "$ops" == c,u,d ]] || ((SECONDS - started > TIMEOUT)); do
  sleep 10
  ops="$(trino_ro "SELECT array_join(array_agg(_op ORDER BY _lsn), ',') FROM bronze.orders
    WHERE id = $order AND _cdc_epoch = $epoch")" || ops=""
done
if [[ "$ops" == c,u,d ]]; then
  result PASS "order $order: insert, update, delete in bronze as c,u,d (epoch $epoch) within $((SECONDS - started))s"
else
  result FAIL "order $order: bronze has '${ops}' after ${TIMEOUT}s (want c,u,d in epoch $epoch)"
fi

queries=('freshness_probe_success{table="bronze.heartbeat"}' 'cdc:bronze_heartbeat_stale:minute')
# The cdc SLI records nothing during its warm-up (deploy/platform/slo/base/cdc-sli.prometheusrule.yaml): the first
# 15 minutes after namespace lakehouse is created or the exporter starts. No samples then is a SKIP, not a FAIL.
warmup='(time() - max(process_start_time_seconds{job="freshness-exporter"}) < 900)
  or (time() - max(kube_namespace_created{namespace="lakehouse"}) < 900)'
if ! prom_forward; then
  for query in "${queries[@]}"; do result FAIL "Prometheus: $query not checked (Prometheus not reachable)"; done
  queries=()
fi
for query in ${queries[@]+"${queries[@]}"}; do
  series="$(prom_series "$query")" || series=-1
  if ((series < 0)); then
    result FAIL "Prometheus: query $query failed"
  elif ((series > 0)); then
    result PASS "Prometheus: $query has $series series"
  elif [[ "$query" == cdc:* ]] && (($(prom_series "$warmup" || echo 0) > 0)); then
    echo "SKIP Prometheus: no samples of $query yet (SLI warm-up: lakehouse or the exporter is under 15 minutes old)"
  else
    result FAIL "Prometheus: no samples of $query"
  fi
done

exit "$failed"
