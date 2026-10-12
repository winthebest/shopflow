#!/usr/bin/env bash
# Deliberate fault injection for the stranded-order sweeper (Phase 7 app slot), on a THROWAWAY local cluster only:
# the database is not backed up first because the cluster is deleted afterwards.
#
# Reproduces the ops-slot incident in one controlled step. Under k6 load (every checkout with an Idempotency-Key,
# retried with the same key), settles are blocked for BLOCK_FOR seconds by a constraint on `payments` that every new
# row violates: payments still charges, the settle transaction fails, the order stays `pending` ("settle failed, order
# left pending"). The constraint is then dropped and the sweeper must settle every stranded order with the same charge
# (payments answers the same for the same order). CDC does not see the DDL; the Flink KPI sees payments dip, then
# jump when the sweeper settles.
#
# Safety: the constraint is dropped by an EXIT/INT/TERM trap, whatever happens; ADD and DROP use lock_timeout (2s)
# with retries, because they take an ACCESS EXCLUSIVE lock on `payments` that would otherwise queue behind the load
# and block every checkout behind it. The last step checks the constraint is gone.
#
# Usage: CLUSTER=sf-app scripts/app-settle-fault.sh
#   LOAD_RATE (checkouts/s, 20), LOAD_DURATION (4m), BLOCK_AFTER (60 s of load first), BLOCK_FOR (20 s),
#   DRAIN_TIMEOUT (300 s for the sweeper), RETRIES (k6 retries with the same key, 1). Results in out/app-settle-fault-*.
set -euo pipefail

cd "$(dirname "$0")/.."

CLUSTER="${CLUSTER:-sf-app}"
KUBE_CONTEXT="${KUBE_CONTEXT:-k3d-$CLUSTER}"
[[ "$KUBE_CONTEXT" == k3d-* ]] || { echo "refusing: fault injection is for local k3d clusters only" >&2; exit 2; }
https_port="$(CLUSTER="$CLUSTER" bash -c 'source scripts/k3d-lib.sh && echo "$HTTPS_PORT"')"
BASE_URL="https://shop.127.0.0.1.sslip.io:$https_port"
LOAD_RATE="${LOAD_RATE:-20}"
LOAD_DURATION="${LOAD_DURATION:-4m}"
BLOCK_AFTER="${BLOCK_AFTER:-60}"
BLOCK_FOR="${BLOCK_FOR:-20}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-300}"
RETRIES="${RETRIES:-1}"
NS=shop
CONSTRAINT=slot_block_settle

kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }

# Interruptible sleep: bash runs a trap only after the foreground command ends, so a plain `sleep 300` would hold the
# settle block in place until it finished; `wait` returns as soon as a trapped signal arrives.
pause() {
  sleep "$1" &
  wait "$!"
}

failed=0
result() {
  if [[ "$1" == PASS ]]; then echo "PASS $2"; else echo "FAIL $2" >&2; failed=1; fi
}

# psql as shop_app (owner of the shop tables) on the CNPG primary; SQL on stdin, the password never on a command line.
psql_shop() {
  local pod
  pod="$(kc -n "$NS" get pod -l cnpg.io/cluster=shop-db,cnpg.io/instanceRole=primary -o name | head -1)"
  { kc -n "$NS" get secret shop-db-app -o jsonpath='{.data.password}' | base64 -d; echo; cat; } \
    | kc -n "$NS" exec -i "$pod" -c postgres -- sh -c \
      'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U shop_app -d shop -v ON_ERROR_STOP=1 -qAt'
}

# DDL on `payments` under load: give up on the lock after 2s and try again, instead of queueing every checkout behind it.
ddl() {
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if psql_shop <<< "SET lock_timeout = '2s'; $1" > /dev/null 2>&1; then
      return 0
    fi
    echo "  $2: lock not granted (attempt $attempt), retrying" >&2
    pause 1
  done
  return 1
}

block_present() { [[ "$(psql_shop <<< "SELECT count(*) FROM pg_constraint WHERE conname = '$CONSTRAINT';")" != 0 ]]; }

K6_PID=""
# shellcheck disable=SC2329 # invoked by the EXIT trap below
cleanup() {
  ddl "ALTER TABLE payments DROP CONSTRAINT IF EXISTS $CONSTRAINT;" "drop" ||
    echo "WARNING: could not drop $CONSTRAINT on payments; run it by hand" >&2
  if [[ -n "$K6_PID" ]]; then kill "$K6_PID" 2> /dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM # through EXIT: the drop always runs

out="out/app-settle-fault-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
start_ts="$(psql_shop <<< "SELECT now();")"
echo "== settle fault on $KUBE_CONTEXT: $LOAD_RATE checkouts/s for $LOAD_DURATION, settles blocked ${BLOCK_FOR}s after \
${BLOCK_AFTER}s (results in $out/)"
ddl "ALTER TABLE payments DROP CONSTRAINT IF EXISTS $CONSTRAINT;" "pre-clean" # a leftover from an aborted run

# k6 directly in the background, so $! is k6 itself (not a subshell) and the trap can stop it.
k6 run --quiet --insecure-skip-tls-verify --log-format raw --console-output "$out/acks.jsonl" \
  --summary-export "$out/k6-summary.json" -e BASE_URL="$BASE_URL" -e RATE="$LOAD_RATE" -e DURATION="$LOAD_DURATION" \
  -e RETRIES="$RETRIES" loadtest/checkout.js > "$out/k6.log" 2>&1 &
K6_PID=$!

pause "$BLOCK_AFTER"
ddl "ALTER TABLE payments ADD CONSTRAINT $CONSTRAINT CHECK (false) NOT VALID;" "block" ||
  { result FAIL "could not take the lock to block settles"; exit 1; }
blocked_at="$(psql_shop <<< "SELECT now();")"
echo "== settles blocked at $blocked_at"
pause "$BLOCK_FOR"
ddl "ALTER TABLE payments DROP CONSTRAINT $CONSTRAINT;" "unblock" || { result FAIL "could not drop $CONSTRAINT"; exit 1; }
unblocked=$SECONDS
echo "== settles unblocked"

# Orders the block stranded: created before the unblock, still pending, no payment row.
psql_shop <<< "SELECT id FROM orders o WHERE status = 'pending' AND created_at < now()
  AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id) ORDER BY id;" > "$out/stranded.txt"
stranded="$(wc -l < "$out/stranded.txt" | tr -d ' ')"
if ((stranded > 0)); then
  result PASS "the block stranded $stranded orders (charged or not, never settled)"
else
  result FAIL "the block stranded no order: nothing to prove (raise LOAD_RATE or BLOCK_FOR)"
fi

# The sweeper claims orders untouched for 30 s, then settles them.
drained_after=""
until [[ -n "$drained_after" ]] || ((SECONDS - unblocked > DRAIN_TIMEOUT)); do
  left="$(psql_shop <<< "SELECT count(*) FROM orders WHERE status = 'pending' AND created_at < '$blocked_at'::timestamptz
    + make_interval(secs => $BLOCK_FOR);")"
  if [[ "$left" == 0 ]]; then drained_after=$((SECONDS - unblocked)); else pause 5; fi
done
if [[ -n "$drained_after" ]]; then
  result PASS "sweeper settled every stranded order ${drained_after}s after the unblock"
else
  result FAIL "orders from the block still pending ${DRAIN_TIMEOUT}s after the unblock"
fi
wait "$K6_PID" || true # the load finishes on its own
K6_PID=""

# Same charge: each recovered order's provider_ref must be the charge_id its failed settle logged.
kc -n "$NS" logs -l app.kubernetes.io/name=orders --prefix=false --tail=-1 --since-time="$start_ts" 2> /dev/null \
  | jq -rc 'select(.message? == "settle failed, order left pending") | [.order_id, (.charge_id // "")] | @tsv' \
  2> /dev/null | sort -u > "$out/settle-failed.tsv" || true
logged="$(wc -l < "$out/settle-failed.tsv" | tr -d ' ')"
mismatched=0
if ((logged > 0)); then
  values="$(awk -F'\t' '$2 != "" {printf "%s(%s,'"'"'%s'"'"')", (n++ ? "," : ""), $1, $2}' "$out/settle-failed.tsv")"
  if [[ -n "$values" ]]; then
    mismatched="$(psql_shop <<< "SELECT count(*) FROM (VALUES $values) AS l(order_id, charge_id)
      LEFT JOIN payments p ON p.order_id = l.order_id WHERE p.provider_ref IS DISTINCT FROM l.charge_id;")"
  fi
fi
if ((logged > 0 && mismatched == 0)); then
  result PASS "$logged failed settles logged; every recovered order kept the charge its request made"
else
  result FAIL "same charge: $logged failed settles logged, $mismatched with a different or missing payment"
fi

# Idempotency under retries: one order per key, every k6 checkout sent one, every ack got its own key's order.
read -r keyless dup_keys < <(psql_shop <<< "SELECT
  (SELECT count(*) FROM orders WHERE created_at >= '$start_ts' AND idempotency_key IS NULL),
  (SELECT count(*) FROM (SELECT idempotency_key FROM orders WHERE idempotency_key IS NOT NULL
    GROUP BY 1 HAVING count(*) > 1) d);" | tr '|' ' ')
jq -rc '.ack? // empty | [.order_id, .idempotency_key] | @tsv' "$out/acks.jsonl" > "$out/acks.tsv" 2> /dev/null || true
acked="$(wc -l < "$out/acks.tsv" | tr -d ' ')"
wrong_order=0
if ((acked > 0)); then
  values="$(awk -F'\t' '{printf "%s(%s,'"'"'%s'"'"')", (n++ ? "," : ""), $1, $2}' "$out/acks.tsv")"
  wrong_order="$(psql_shop <<< "SELECT count(*) FROM (VALUES $values) AS a(order_id, idempotency_key)
    LEFT JOIN orders o ON o.id = a.order_id WHERE o.idempotency_key IS DISTINCT FROM a.idempotency_key;")"
fi
retries="$(jq '.metrics.checkout_retries.count // 0' "$out/k6-summary.json" 2> /dev/null || echo 0)"
if [[ "$keyless" == 0 && "$dup_keys" == 0 && "$wrong_order" == 0 ]] && ((acked > 0)); then
  result PASS "idempotency: $acked acks each got their own key's order, $retries retries, 0 keyless, 0 duplicate keys"
else
  result FAIL "idempotency: keyless $keyless, duplicate keys $dup_keys, acks with another key's order $wrong_order"
fi

if block_present; then result FAIL "$CONSTRAINT is still on payments"; else result PASS "$CONSTRAINT removed"; fi
jq -n --argjson stranded "$stranded" --argjson drained "${drained_after:-null}" --argjson logged "$logged" \
  --argjson mismatched "$mismatched" --argjson acked "$acked" --argjson retries "$retries" \
  --argjson rate "$LOAD_RATE" --argjson block_for "$BLOCK_FOR" \
  '{load_checkouts_per_s: $rate, blocked_s: $block_for, stranded: $stranded, drained_s_after_unblock: $drained,
    failed_settles_logged: $logged, different_charge: $mismatched, acks: $acked, k6_retries: $retries}' \
  > "$out/summary.json"
echo "== results: $out/summary.json"
exit "$failed"
