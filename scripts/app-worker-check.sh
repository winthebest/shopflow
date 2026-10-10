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
#      ScaledObject's minReplicaCount after the load; both times are measured;
#   6. once the consumer lag is 0: no order has two shipments (HAVING count(*) > 1), every paid order has exactly one
#      shipment, and no shipment belongs to an order that is not paid. Run it again after the re-snapshot runbook:
#      a full replay of the topic must not create duplicates.
# Postgres is read as shop_app (password on stdin, never on a command line); Kafka only through the worker pod.
# Usage: CLUSTER=sf-main scripts/app-worker-check.sh   (or make app-worker-check CLUSTER=sf-main)
#   KUBE_CONTEXT overrides k3d-<CLUSTER>; BASE_URL the gateway (default https://shop.127.0.0.1.sslip.io:<port>);
#   LOAD_RATE (checkouts/s, 80), LOAD_DURATION (3m), SCALE_TARGET (3), SCALE_DOWN_TIMEOUT (600s), DRAIN_TIMEOUT (600s).
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
SCALE_TARGET="${SCALE_TARGET:-3}"
SCALE_DOWN_TIMEOUT="${SCALE_DOWN_TIMEOUT:-600}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-600}"
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

# Consumer group of the worker, seen with the worker's own credentials from inside its pod:
# JSON {"state", "members", "clients", "lag"} (lag = end offset - committed offset, summed over partitions).
group_probe() {
  kc -n "$NS" exec -i "deploy/$WORKER" -c "$WORKER" -- python -I - <<'PY'
import asyncio, json, os
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.helpers import create_ssl_context

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
        lag = sum(end[tp] - (committed[tp].offset if tp in committed else begin[tp]) for tp in partitions)
        print(json.dumps({"state": state, "members": len(members), "clients": sorted({m[1] for m in members}),
                          "partitions": len(partitions), "lag": lag}))
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

# 5. Scaling under load.
if [[ "${SKIP_LOAD:-0}" == 1 ]]; then
  echo "SKIP scaling under load (SKIP_LOAD=1)"
else
  out="out/app-worker-check-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$out"
  echo "== load: $LOAD_RATE checkouts/s for $LOAD_DURATION on $BASE_URL (k6 log in $out/)"
  k6 run --quiet --insecure-skip-tls-verify --log-format raw --console-output "$out/acks.jsonl" \
    --summary-export "$out/k6-summary.json" -e BASE_URL="$BASE_URL" -e RATE="$LOAD_RATE" -e DURATION="$LOAD_DURATION" \
    loadtest/checkout.js > "$out/k6.log" 2>&1 &
  K6_PID=$!
  started=$SECONDS
  peak=0
  scaled_up_after=""
  while kill -0 "$K6_PID" 2> /dev/null; do
    current="$(replicas)"
    ((current > peak)) && peak=$current
    if [[ -z "$scaled_up_after" ]] && ((current >= SCALE_TARGET)); then scaled_up_after=$((SECONDS - started)); fi
    sleep 5
  done
  k6_status=0
  wait "$K6_PID" || k6_status=$?
  K6_PID=""
  load_ended=$SECONDS
  ((k6_status == 0)) || echo "note: k6 exited with $k6_status (thresholds or errors, see $out/k6.log)" >&2
  if [[ -n "$scaled_up_after" ]]; then
    result PASS "scale up: $SCALE_TARGET ready replicas ${scaled_up_after}s after the load started (peak $peak)"
  else
    result FAIL "scale up: peak $peak ready replicas, never $SCALE_TARGET (raise LOAD_RATE or the chart's latency)"
  fi
  scaled_down_after=""
  until [[ -n "$scaled_down_after" ]] || ((SECONDS - load_ended > SCALE_DOWN_TIMEOUT)); do
    desired="$(replicas spec.replicas)"
    if ((desired > 0 && desired <= min_replicas)); then
      scaled_down_after=$((SECONDS - load_ended))
    else
      sleep 10
    fi
  done
  if [[ -n "$scaled_down_after" ]]; then
    result PASS "scale down: back to $min_replicas replica(s) ${scaled_down_after}s after the load ended"
  else
    result FAIL "scale down: still above $min_replicas replica(s) ${SCALE_DOWN_TIMEOUT}s after the load ended"
  fi
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
counts="$(psql_shop 2> /dev/null <<'SQL' | tr '\n' ' ' || true
SELECT count(*) FROM (SELECT order_id FROM shipments GROUP BY order_id HAVING count(*) > 1) AS d;
SELECT count(*) FROM orders o WHERE o.status = 'paid' AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.order_id = o.id);
SELECT count(*) FROM shipments s JOIN orders o ON o.id = s.order_id WHERE o.status <> 'paid';
SELECT count(*) FROM shipments;
SQL
)"
if [[ ! "$counts" =~ ^[0-9]+\ [0-9]+\ [0-9]+\ [0-9]+\ ?$ ]]; then
  result FAIL "shipments: could not query Postgres as shop_app (got '${counts}')"
  exit 1
fi
read -r duplicates unshipped wrong total <<< "$counts"
if ((duplicates == 0)); then
  result PASS "no order has more than one shipment ($total shipments)"
else
  result FAIL "$duplicates order(s) have more than one shipment"
fi
if ((unshipped == 0)); then
  result PASS "every paid order has a shipment"
else
  result FAIL "$unshipped paid order(s) have no shipment"
fi
if ((wrong == 0)); then
  result PASS "no shipment for an order that is not paid"
else
  result FAIL "$wrong shipment(s) belong to orders that are not paid"
fi

exit "$failed"
