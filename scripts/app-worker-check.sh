#!/usr/bin/env bash
# Phase 7 acceptance of fulfillment-worker on a running cluster with profiles core + data + ops (KEDA). Reused at
# Gate 3 and in the KEDA game day. Prints PASS/FAIL per check; exits 1 if any failed.
#   1. the two Sync-hook Jobs copied the KafkaUser password and the cluster CA into namespace shop (keys checked,
#      values never printed);
#   2. Deployment fulfillment-worker is Available (readiness = consume loop alive + database reachable);
#   3. the worker is a member of consumer group fulfillment-worker: checked from inside its pod, with its own
#      SASL_SSL credentials and the cluster CA, i.e. the exact path the worker uses;
#   4. ScaledObject fulfillment-worker is Ready (KEDA reached Kafka); its scaler protocol version is shown;
#   5. scaling (skip with SKIP_LOAD=1): k6 drives checkouts above one replica's throughput (the chart's
#      SHIPMENT_LATENCY_MS sets it: 20ms -> ~50 shipments/s), replicas reach SCALE_TARGET, then return to the
#      ScaledObject's minReplicaCount after the load. Results for the PR / game day in out/app-worker-check-<UTC>/:
#      timeline.csv (replicas, lag, committed offsets every ~5s), summary.json and a Markdown table on stdout with
#      the scale-up/scale-down times, the measured records/s per replica and the lagThreshold it suggests (the
#      backlog one replica clears in LAG_BUDGET_SECONDS; ADR 0304 sizes lagThreshold from this measurement);
#      WATCH=1 measures the same without k6, for a backlog that appears by itself (game day 3: Kafka Connect down,
#      then back): it waits up to WATCH_TIMEOUT for the lag to rise above 0, then samples until it is drained and the
#      replicas are back to the minimum; start it while the lag is still 0;
#   6. once the consumer lag is 0: no order has two shipments (HAVING count(*) > 1), every order paid more than
#      SHIP_GRACE_SECONDS ago has exactly one shipment, and no shipment belongs to an order that is not paid.
# Re-snapshot (idempotency under a full replay): run with SAVE_BASELINE=<file> before the re-snapshot runbook, then
# with REPLAY_BASELINE=<file> after it. The second run passes only if every order of the baseline came back as a
# snapshot read (_op "r") with a newer _cdc_epoch and the worker consumed it: without that, "no duplicates" would be
# true simply because nothing was replayed.
# Postgres is read as shop_app (password on stdin, never on a command line); Kafka only through the worker pod.
# Usage: CLUSTER=sf-main scripts/app-worker-check.sh   (or make app-worker-check CLUSTER=sf-main)
#   KUBE_CONTEXT overrides k3d-<CLUSTER>; BASE_URL the gateway (default https://shop.127.0.0.1.sslip.io:<port>);
#   LOAD_RATE (checkouts/s, 80), LOAD_DURATION (3m), SCALE_TARGET (2), SCALE_DOWN_TIMEOUT (600s), DRAIN_TIMEOUT (600s),
#   LAG_BUDGET_SECONDS (30), WATCH_TIMEOUT (900s), SHIP_GRACE_SECONDS (60: orders paid more recently may still be in
#   flight while load runs), SKIP_LOAD=1, WATCH=1, SAVE_BASELINE / REPLAY_BASELINE=<file>.
set -euo pipefail

cd "$(dirname "$0")/.."

CLUSTER="${CLUSTER:-sf-main}"
KUBE_CONTEXT="${KUBE_CONTEXT:-k3d-$CLUSTER}"
case "$CLUSTER" in # HTTPS load-balancer ports from docs/contracts/environment.md
  sf-main) https_port=9443 ;;
  sf-platform) https_port=8443 ;;
  sf-sre) https_port=8444 ;;
  sf-data) https_port=8445 ;;
  sf-app) https_port=8446 ;;
  *) https_port=443 ;;
esac
BASE_URL="${BASE_URL:-https://shop.127.0.0.1.sslip.io:$https_port}"
LOAD_RATE="${LOAD_RATE:-80}"
LOAD_DURATION="${LOAD_DURATION:-3m}"
SCALE_TARGET="${SCALE_TARGET:-2}" # lagThreshold 3000: steady 80/s needs 2; a backlog burst reaches the maximum
SCALE_DOWN_TIMEOUT="${SCALE_DOWN_TIMEOUT:-600}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-600}"
LAG_BUDGET_SECONDS="${LAG_BUDGET_SECONDS:-30}"
SHIP_GRACE_SECONDS="${SHIP_GRACE_SECONDS:-60}"
[[ "$SHIP_GRACE_SECONDS" =~ ^[0-9]+$ ]] || { echo "SHIP_GRACE_SECONDS must be whole seconds" >&2; exit 2; }
WATCH_TIMEOUT="${WATCH_TIMEOUT:-900}"
NS=shop
WORKER=fulfillment-worker

kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }

failed=0
result() {
  if [[ "$1" == PASS ]]; then echo "PASS $2"; else echo "FAIL $2" >&2; failed=1; fi
}

# psql as shop_app on the CNPG primary; SQL on stdin, one value per line out.
psql_shop() {
  local pod
  pod="$(kc -n "$NS" get pod -l cnpg.io/cluster=shop-db,cnpg.io/instanceRole=primary -o name | head -1)"
  { kc -n "$NS" get secret shop-db-app -o jsonpath='{.data.password}' | base64 -d; echo; cat; } \
    | kc -n "$NS" exec -i "$pod" -c postgres -- sh -c \
      'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U shop_app -d shop -v ON_ERROR_STOP=1 -qAt'
}

# Consumer group of the worker, seen with the worker's own credentials from inside its pod. Prints JSON summed over
# partitions: committed = the group's committed offsets (the beginning of a partition it never committed), lag = end
# offsets - committed, plus end_offsets per partition. Modes (first argument):
#   group     (default) just that;
#   baseline  + epoch: the newest _cdc_epoch, read from the last record of each partition;
#   replay    + replayed_orders / replay_complete: orders re-emitted as snapshot reads (_op "r") with an epoch newer
#             than the baseline's, among the records written since the baseline offsets (2nd argument: baseline JSON).
group_probe() {
  kc -n "$NS" exec -i "deploy/$WORKER" -c "$WORKER" -- python -I - "$@" <<'PY'
import asyncio, json, os, sys, time
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.helpers import create_ssl_context

MODE = sys.argv[1] if len(sys.argv) > 1 else "group"

def auth():
    settings = {"bootstrap_servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"]}
    if os.environ.get("KAFKA_SECURITY_PROTOCOL", "SASL_SSL") == "SASL_SSL":  # always on the cluster
        settings |= {
            "security_protocol": "SASL_SSL",
            "sasl_mechanism": "SCRAM-SHA-512",
            "sasl_plain_username": os.environ["KAFKA_USERNAME"],
            "sasl_plain_password": os.environ["KAFKA_PASSWORD"],
            "ssl_context": create_ssl_context(cafile=os.environ["KAFKA_CA_FILE"]),
        }
    return settings

def change(value):
    """(op, order id, epoch) of an orders change record, or None."""
    try:
        data = json.loads(value)
        return data["_op"], int(data["id"]), int(data["_cdc_epoch"])
    except (TypeError, ValueError, KeyError):
        return None

async def read(consumer, start, end, keep):
    """Pass every record in [start[tp], end[tp]) to keep(); True if all were read within 120s."""
    tps = [tp for tp in end if end[tp] > start[tp]]
    if not tps:
        return True
    consumer.assign(tps)
    for tp in tps:
        consumer.seek(tp, start[tp])
    remaining, deadline = set(tps), time.monotonic() + 120
    while remaining and time.monotonic() < deadline:
        batch = await consumer.getmany(*remaining, timeout_ms=1000, max_records=5000)
        for tp, records in batch.items():
            for record in records:
                if record.offset < end[tp]:
                    keep(record)
            if records and records[-1].offset >= end[tp] - 1:
                remaining.discard(tp)
    return not remaining

async def main():
    group, topic = os.environ["KAFKA_GROUP_ID"], os.environ["KAFKA_TOPIC"]
    admin, probe = AIOKafkaAdminClient(**auth()), AIOKafkaConsumer(**auth())
    await admin.start()
    await probe.start()
    try:
        _, _, state, _, _, members = (await admin.describe_consumer_groups([group]))[0].groups[0]
        committed = await admin.list_consumer_group_offsets(group)
        [described] = await admin.describe_topics([topic])
        partitions = sorted(TopicPartition(topic, p["partition"]) for p in described["partitions"])
        begin, end = await probe.beginning_offsets(partitions), await probe.end_offsets(partitions)
        done = sum(committed[tp].offset if tp in committed else begin[tp] for tp in partitions)
        result = {"state": state, "members": len(members), "clients": sorted({m[1] for m in members}),
                  "partitions": len(partitions), "lag": sum(end.values()) - done, "committed": done,
                  "end_offsets": {str(tp.partition): end[tp] for tp in partitions}}
        if MODE == "baseline":
            epochs = []
            def keep_epoch(record):
                if (c := change(record.value)) is not None:
                    epochs.append(c[2])
            await read(probe, {tp: max(begin[tp], end[tp] - 1) for tp in partitions}, end, keep_epoch)
            result["epoch"] = max(epochs, default=None)
        elif MODE == "replay":
            base = json.loads(sys.argv[2])
            base_epoch = base["epoch"] if base["epoch"] is not None else -1
            ids = set()
            def keep_snapshot(record):
                c = change(record.value)
                if c is not None and c[0] == "r" and c[2] > base_epoch:
                    ids.add(c[1])
            since = {tp: max(begin[tp], base["end_offsets"].get(str(tp.partition), 0)) for tp in partitions}
            result["replay_complete"] = await read(probe, since, end, keep_snapshot)
            result["replayed_orders"] = len(ids)
        print(json.dumps(result))
    finally:
        await probe.stop()
        await admin.close()

asyncio.run(main())
PY
}

# Ready / desired replicas; 0 when unknown (never fails, so a flaky API call cannot abort the check).
replicas() {
  local value
  value="$(kc -n "$NS" get deploy "$WORKER" -o jsonpath="{.${1:-status.readyReplicas}}" 2> /dev/null || true)"
  echo "${value:-0}"
}

# lagThreshold of the ScaledObject's Kafka trigger (0 when unknown).
lag_threshold() {
  local value
  value="$(kc -n "$NS" get scaledobject "$WORKER" -o jsonpath='{.spec.triggers[0].metadata.lagThreshold}' \
    2> /dev/null || true)"
  [[ "$value" =~ ^[0-9]+$ ]] && echo "$value" || echo 0
}

K6_PID=""
trap 'if [[ -n "$K6_PID" ]]; then kill "$K6_PID" 2> /dev/null || true; fi' EXIT

echo "== fulfillment-worker on $KUBE_CONTEXT"

# 1. Credential copies (Sync hooks of app fulfillment-worker).
for job in credentials ca; do
  if [[ "$(kc -n "$NS" get job "$WORKER-kafka-copy-$job" -o jsonpath='{.status.succeeded}' 2> /dev/null)" == 1 ]]; then
    result PASS "copy Job $WORKER-kafka-copy-$job completed"
  else
    result FAIL "copy Job $WORKER-kafka-copy-$job did not complete"
  fi
done
keys() { kc -n "$NS" get secret "$1" -o json 2> /dev/null | jq -r '.data // {} | keys | join(",")' || true; }
credential_keys="$(keys "$WORKER-kafka")"
ca_keys="$(keys "$WORKER-kafka-ca")"
if [[ ",$credential_keys," == *,password,* && ",$credential_keys," == *,username,* && ",$ca_keys," == *,ca.crt,* ]]; then
  result PASS "copied Secrets hold username/password and ca.crt"
else
  result FAIL "copied Secrets: $WORKER-kafka has [$credential_keys], $WORKER-kafka-ca has [$ca_keys]"
fi

# 2. Deployment available.
if kc -n "$NS" rollout status "deploy/$WORKER" --timeout=180s > /dev/null 2>&1; then
  result PASS "deployment $WORKER available ($(replicas) ready)"
else
  result FAIL "deployment $WORKER not available"
fi

# 3. Group membership over SASL_SSL.
group="$(group_probe 2> /dev/null)" || group=""
if [[ -n "$group" ]] && (($(jq '.members' <<< "$group") > 0)); then
  result PASS "SASL_SSL: group $WORKER $(jq -r '.state' <<< "$group") with $(jq '.members' <<< "$group") member(s) on \
$(jq '.partitions' <<< "$group") partitions, lag $(jq '.lag' <<< "$group")"
else
  result FAIL "SASL_SSL: no member in group $WORKER (probe output: '${group}')"
fi

# 4. KEDA ScaledObject.
so_ready="$(kc -n "$NS" get scaledobject "$WORKER" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' \
  2> /dev/null || true)"
so_version="$(kc -n "$NS" get scaledobject "$WORKER" -o jsonpath='{.spec.triggers[0].metadata.version}' 2> /dev/null \
  || true)"
min_replicas="$(kc -n "$NS" get scaledobject "$WORKER" -o jsonpath='{.spec.minReplicaCount}' 2> /dev/null || true)"
min_replicas="${min_replicas:-1}"
if [[ "$so_ready" == True ]]; then
  result PASS "ScaledObject $WORKER Ready (Kafka scaler protocol version ${so_version:-default}, min $min_replicas)"
else
  result FAIL "ScaledObject $WORKER not Ready (status '${so_ready}', version ${so_version:-default})"
fi

# Replay baseline, before a re-snapshot: what the topic holds now and how many orders exist.
if [[ -n "${SAVE_BASELINE:-}" ]]; then
  baseline="$(group_probe baseline 2> /dev/null || true)"
  orders="$(psql_shop 2> /dev/null <<< 'SELECT count(*) FROM orders;' || true)"
  if jq -e '.end_offsets and (.epoch | type == "number")' <<< "$baseline" > /dev/null 2>&1 \
    && [[ "$orders" =~ ^[0-9]+$ ]]; then
    mkdir -p "$(dirname "$SAVE_BASELINE")"
    jq --argjson orders "$orders" '{end_offsets, epoch, orders: $orders}' <<< "$baseline" > "$SAVE_BASELINE"
    result PASS "replay baseline saved to $SAVE_BASELINE: $orders orders, newest epoch $(jq '.epoch' <<< "$baseline")"
  else
    result FAIL "replay baseline: could not read the topic or count orders (probe '${baseline}', orders '${orders}')"
  fi
fi

# 5. Scaling: under k6 load (default), or with WATCH=1 while a backlog that appears by itself is drained (e.g. Kafka
#    Connect resuming after game day 3). SKIP_LOAD=1 without WATCH=1 skips it.
if [[ "${SKIP_LOAD:-0}" == 1 && "${WATCH:-0}" != 1 ]]; then
  echo "SKIP scaling (SKIP_LOAD=1)"
else
  out="out/app-worker-check-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$out"
  timeline="$out/timeline.csv"
  echo "elapsed_s,phase,desired,ready,lag,committed" > "$timeline"
  started=$SECONDS
  # Appends one timeline row and prints "desired ready lag"; lag and committed are -1 when the probe failed.
  sample() {
    local desired ready probe lag committed
    desired="$(replicas spec.replicas)"
    ready="$(replicas)"
    probe="$(group_probe 2> /dev/null | jq -r '"\(.lag) \(.committed)"' 2> /dev/null || true)"
    read -r lag committed <<< "$probe"
    [[ "$lag" =~ ^[0-9]+$ && "$committed" =~ ^[0-9]+$ ]] || lag=-1 committed=-1
    echo "$((SECONDS - started)),$1,$desired,$ready,$lag,$committed" >> "$timeline"
    echo "$desired $ready $lag"
  }
  peak=0 max_lag=0 scaled_up_after="" drained_after="" scaled_down_after=""
  # Peak, max lag and the time to SCALE_TARGET ready replicas, counted from $up_from.
  track() {
    ((ready > peak)) && peak=$ready
    ((lag > max_lag)) && max_lag=$lag
    if [[ -z "$scaled_up_after" ]] && ((ready >= SCALE_TARGET)); then scaled_up_after=$((SECONDS - up_from)); fi
    return 0
  }
  settled() { [[ -n "$drained_after" ]] && { [[ -n "$scaled_down_after" ]] || ((peak <= min_replicas)); }; }
  # Samples (phase $1) until the lag is 0 and the replicas are back to the minimum (if they ever left it), timing
  # both from $settle_from; gives up SCALE_DOWN_TIMEOUT seconds after it.
  settle() {
    until settled || ((SECONDS - settle_from > SCALE_DOWN_TIMEOUT)); do
      read -r desired ready lag < <(sample "$1")
      track
      if [[ -z "$drained_after" ]] && ((lag == 0)); then drained_after=$((SECONDS - settle_from)); fi
      if [[ -z "$scaled_down_after" ]] && ((peak > min_replicas && desired > 0 && desired <= min_replicas)); then
        scaled_down_after=$((SECONDS - settle_from))
      fi
      settled || sleep 5
    done
  }

  if [[ "${WATCH:-0}" == 1 ]]; then
    # Started while the lag is still 0 (game day 3: a few minutes before Connect comes back): wait for the backlog.
    mode=backlog up_label="after the backlog appeared" settle_label="after the backlog appeared"
    echo "== watch: waiting up to ${WATCH_TIMEOUT}s for a backlog (lag > 0); results in $out/"
    burst_at=""
    until [[ -n "$burst_at" ]] || ((SECONDS - started > WATCH_TIMEOUT)); do
      read -r desired ready lag < <(sample wait)
      if ((lag > 0)); then burst_at=$SECONDS; else sleep 5; fi
    done
    if [[ -n "$burst_at" ]]; then
      up_from=$burst_at settle_from=$burst_at
      track
      settle backlog
    else
      result FAIL "watch: no backlog appeared within ${WATCH_TIMEOUT}s"
    fi
  else
    mode=load up_label="after the load started" settle_label="after the load ended"
    echo "== load: $LOAD_RATE checkouts/s for $LOAD_DURATION on $BASE_URL (results in $out/)"
    k6 run --quiet --insecure-skip-tls-verify --log-format raw --console-output "$out/acks.jsonl" \
      --summary-export "$out/k6-summary.json" -e BASE_URL="$BASE_URL" -e RATE="$LOAD_RATE" \
      -e DURATION="$LOAD_DURATION" loadtest/checkout.js > "$out/k6.log" 2>&1 &
    K6_PID=$!
    up_from=$started
    while kill -0 "$K6_PID" 2> /dev/null; do
      read -r desired ready lag < <(sample load)
      track
      sleep 5
    done
    k6_status=0
    wait "$K6_PID" || k6_status=$?
    K6_PID=""
    ((k6_status == 0)) || echo "note: k6 exited with $k6_status (thresholds or errors, see $out/k6.log)" >&2
    settle_from=$SECONDS
    settle after
  fi

  if [[ "$mode" == load || -n "${burst_at:-}" ]]; then
    if [[ -n "$scaled_up_after" ]]; then
      result PASS "scale up: $SCALE_TARGET ready replicas ${scaled_up_after}s $up_label (peak $peak)"
    else
      result FAIL "scale up: peak $peak ready replicas, never $SCALE_TARGET (raise LOAD_RATE or the chart's latency)"
    fi
    if ((peak <= min_replicas)); then
      echo "SKIP scale down: never above $min_replicas replica(s)"
    elif [[ -n "$scaled_down_after" ]]; then
      result PASS "scale down: back to $min_replicas replica(s) ${scaled_down_after}s $settle_label"
    else
      result FAIL "scale down: still above $min_replicas replica(s) ${SCALE_DOWN_TIMEOUT}s $settle_label"
    fi
  fi

  # Records/s one replica processes: median over the sample intervals where a backlog of at least 100 records
  # remained at both ends (the replicas were busy the whole interval) of committed-offset growth / s / ready replicas.
  # A lag record is one change event of orders (about 2 per checkout: created pending, then paid or failed).
  load_json=null
  [[ "$mode" == load ]] && load_json="$(jq -nc --argjson rate "$LOAD_RATE" --arg duration "$LOAD_DURATION" \
    '{checkouts_per_s: $rate, duration: $duration}')"
  jq -Rn --arg mode "$mode" --argjson load "$load_json" --arg up_label "$up_label" --arg settle_label "$settle_label" \
    --argjson target "$SCALE_TARGET" --argjson min "$min_replicas" --argjson up "${scaled_up_after:-null}" \
    --argjson down "${scaled_down_after:-null}" --argjson drained "${drained_after:-null}" --argjson peak "$peak" \
    --argjson max_lag "$max_lag" --argjson threshold "$(lag_threshold)" --argjson budget "$LAG_BUDGET_SECONDS" '
    [inputs | split(",") | select(.[0] != "elapsed_s")
     | {t: (.[0] | tonumber), ready: (.[3] | tonumber), lag: (.[4] | tonumber), committed: (.[5] | tonumber)}] as $rows
    | [range(1; $rows | length) as $i | $rows[$i - 1] as $a | $rows[$i] as $b
       | select($a.lag >= 100 and $b.lag >= 100 and $b.t > $a.t and $a.ready + $b.ready > 0)
       | ($b.committed - $a.committed) / ($b.t - $a.t) / (($a.ready + $b.ready) / 2)] as $rates
    | ($rates | sort | if length > 0 then .[length / 2 | floor] else null end) as $per_replica
    | {mode: $mode, load: $load, up_label: $up_label, settle_label: $settle_label, scale_target: $target,
       min_replicas: $min, scale_up_s: $up, peak_ready_replicas: $peak, max_lag: $max_lag, drained_s: $drained,
       scale_down_s: $down, busy_intervals: ($rates | length),
       records_per_s_per_replica: (if $per_replica then ($per_replica * 10 | round) / 10 else null end),
       lag_threshold: $threshold, lag_budget_s: $budget,
       suggested_lag_threshold: (if $per_replica then ([10, ($per_replica * $budget | round)] | max) else null end)}
    ' < "$timeline" > "$out/summary.json"
  echo "== results ($out/summary.json, timeline.csv); paste into the PR:"
  jq -r '
    def s: if . == null then "n/a" else "\(.) s" end;
    "| Measure | Value |", "|---|---|",
    (if .load then "| Load | \(.load.checkouts_per_s) checkouts/s for \(.load.duration) |"
     else "| Trigger | backlog that appeared by itself (watch mode) |" end),
    "| \(.min_replicas) → \(.scale_target) ready replicas | \(.scale_up_s | s) \(.up_label) (peak \(.peak_ready_replicas)) |",
    "| Max consumer lag | \(.max_lag) records |",
    "| Lag drained | \(.drained_s | s) \(.settle_label) |",
    "| Back to \(.min_replicas) replica(s) | \(.scale_down_s | s) \(.settle_label) |",
    "| Throughput per replica | \(.records_per_s_per_replica // "n/a") records/s (\(.busy_intervals) busy intervals) |",
    "| lagThreshold | \(.lag_threshold) now; \(.suggested_lag_threshold // "n/a") suggested (\(.lag_budget_s) s of work per replica) |"
  ' "$out/summary.json"
fi

# 6. Idempotency, once the worker has caught up.
lag=-1
deadline=$((SECONDS + DRAIN_TIMEOUT))
until ((lag == 0)) || ((SECONDS > deadline)); do
  lag="$(group_probe 2> /dev/null | jq '.lag')" || lag=-1
  ((lag == 0)) || sleep 10
done
if ((lag == 0)); then
  result PASS "consumer lag drained to 0"
else
  result FAIL "consumer lag not drained after ${DRAIN_TIMEOUT}s (last: $lag)"
fi
# After a re-snapshot: "no duplicates" proves idempotency only if the worker really read the replay. Every order of
# the baseline must have come back as a snapshot read (_op "r") with a newer epoch, and the lag must be 0.
if [[ -n "${REPLAY_BASELINE:-}" ]]; then
  base_orders="$(jq -r '.orders' "$REPLAY_BASELINE" 2> /dev/null || true)"
  replay="$(group_probe replay "$(jq -c '{end_offsets, epoch}' "$REPLAY_BASELINE" 2> /dev/null)" 2> /dev/null || true)"
  replayed="$(jq -r '.replayed_orders // empty' <<< "$replay" 2> /dev/null || true)"
  complete="$(jq -r '.replay_complete // empty' <<< "$replay" 2> /dev/null || true)"
  if [[ ! "$base_orders" =~ ^[0-9]+$ || ! "$replayed" =~ ^[0-9]+$ ]]; then
    result FAIL "replay: could not read the baseline $REPLAY_BASELINE or the topic (probe '${replay}')"
  elif [[ "$complete" != true ]] || ((replayed < base_orders)); then
    result FAIL "replay NOT observed: $replayed of $base_orders orders came back as snapshot records with a newer \
epoch (topic read complete: ${complete:-no}); the re-snapshot did not reach the topic, so the shipment checks below \
prove nothing about idempotency"
  elif ((lag != 0)); then
    result FAIL "replay in the topic ($replayed orders) but not consumed yet (lag $lag): the shipment checks below do \
not cover it"
  else
    result PASS "replay observed: $replayed orders re-read as snapshot records with a newer epoch (baseline \
$base_orders), all consumed by the worker"
  fi
fi
# Paid within the last SHIP_GRACE_SECONDS: possibly still in flight while load runs (CDC + worker take seconds), so
# reported, not failed. SHIP_GRACE_SECONDS is validated as digits above.
counts="$(psql_shop 2> /dev/null <<SQL | tr '\n' ' ' || true
SELECT count(*) FROM (SELECT order_id FROM shipments GROUP BY order_id HAVING count(*) > 1) AS d;
SELECT count(*) FROM orders o WHERE o.status = 'paid' AND o.updated_at < now() - make_interval(secs => $SHIP_GRACE_SECONDS)
  AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.order_id = o.id);
SELECT count(*) FROM shipments s JOIN orders o ON o.id = s.order_id WHERE o.status <> 'paid';
SELECT count(*) FROM shipments;
SELECT count(*) FROM orders o WHERE o.status = 'paid' AND o.updated_at >= now() - make_interval(secs => $SHIP_GRACE_SECONDS)
  AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.order_id = o.id);
SQL
)"
if [[ ! "$counts" =~ ^[0-9]+\ [0-9]+\ [0-9]+\ [0-9]+\ [0-9]+\ ?$ ]]; then
  result FAIL "shipments: could not query Postgres as shop_app (got '${counts}')"
  exit 1
fi
read -r duplicates unshipped wrong total in_flight <<< "$counts"
if ((duplicates == 0)); then
  result PASS "no order has more than one shipment ($total shipments)"
else
  result FAIL "$duplicates order(s) have more than one shipment"
fi
if ((unshipped == 0)); then
  result PASS "every order paid more than ${SHIP_GRACE_SECONDS}s ago has a shipment ($in_flight newer ones in flight)"
else
  result FAIL "$unshipped order(s) paid more than ${SHIP_GRACE_SECONDS}s ago have no shipment"
fi
if ((wrong == 0)); then
  result PASS "no shipment for an order that is not paid"
else
  result FAIL "$wrong shipment(s) belong to orders that are not paid"
fi

exit "$failed"
