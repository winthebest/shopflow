#!/usr/bin/env bash
# CDC epoch tooling (docs/adr/0406). An epoch is one "life" of the CDC pipeline: every `make up`, restore or
# re-snapshot starts a new one, bronze rows carry it in _cdc_epoch, and silver reads only the newest epoch whose
# snapshot completed.
#
# Usage:
#   scripts/cdc-epoch.sh new  [--epoch N] [--timeout S]  before the connectors sync (make up, after the root apps)
#   scripts/cdc-epoch.sh wait [--epoch N] [--timeout S]  once Postgres and Connect run: record N in meta.cdc_epochs
#
# new   Writes Secret kafka/cdc-epoch (key epoch, read by the connectors at task start) and KafkaTopic
#       iceberg-control-<N> (the Iceberg sink's control topic for this epoch). Needs only the Strimzi CRDs, not a
#       running Kafka. Default N = unix time in seconds: an int4 until 2038, above every earlier local epoch.
#       Prints N. Connectors already running keep their old epoch until restarted (the re-snapshot runbook
#       restarts them). With profile rt it also deletes FlinkDeployment flink/kpi-minute, which Argo CD recreates for
#       the new epoch from fresh state. On AWS, ESO writes the Secret from SSM and only `wait` is used.
# wait  Inserts N into meta.cdc_epochs (as shop_app), waits until Debezium reports the snapshot as completed
#       (debezium_metrics_snapshotcompleted{context="snapshot",name="shop"} == 1 on the Connect metrics port),
#       then sets snapshot_completed_at. Use it only for an epoch that snapshots (new Kafka or deleted offsets):
#       with snapshot.mode=when_needed a restart with existing offsets never snapshots.
#       N defaults to the value in Secret kafka/cdc-epoch.
#
# Cluster: CLUSTER (default sf-main) selects context k3d-<CLUSTER>, like scripts/k3d-*.sh; KUBE_CONTEXT overrides
# it (e.g. EKS). Test seam: CDC_EPOCH_PSQL (SQL on stdin, as shop_app in database shop) and CDC_EPOCH_METRICS
# (prints the Connect metrics) replace the kubectl-based defaults; the Connect smoke test sets them to docker compose.
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-k3d-${CLUSTER:-sf-main}}"
CONNECT_NAMESPACE=kafka
CONNECT_CLUSTER=cdc          # KafkaConnect name (deploy/platform/kafka-connect)
KAFKA_CLUSTER=shopflow       # Kafka name (deploy/platform/kafka)
DEBEZIUM_SERVER=shop         # Debezium topic.prefix, the `name` label of its metrics
FLINK_NAMESPACE=flink
FLINK_JOB=kpi-minute        # FlinkDeployment (deploy/platform/flink), profile rt
DB_NAMESPACE=shop
DB_CLUSTER=shop-db           # CNPG Cluster; shop_app's password is in Secret shop-db-app

log() { printf '[cdc-epoch] %s\n' "$*" >&2; }
die() {
  log "error: $*"
  exit 1
}
usage() { die "usage: $0 new|wait [--epoch N] [--timeout SECONDS]"; }
kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }

command="${1:-}"
[[ "$command" == new || "$command" == wait ]] || usage
shift
epoch=""
timeout=600
while (($#)); do
  case "$1" in
    --epoch) epoch="${2:?--epoch needs a value}" ;;
    --timeout) timeout="${2:?--timeout needs a value}" ;;
    *) usage ;;
  esac
  shift 2
done
deadline=$((SECONDS + timeout))

valid_epoch() { [[ "$1" =~ ^[1-9][0-9]{0,9}$ ]] && (($1 <= 2147483647)); }

# until_ok <what> <command...>: retry every 5s until the command succeeds or the timeout expires.
until_ok() {
  local what="$1"
  shift
  until "$@"; do
    ((SECONDS < deadline)) || die "timed out after ${timeout}s waiting for $what"
    sleep 5
  done
}

# SQL on stdin, as shop_app in database shop. The password goes through stdin, never through argv.
run_sql() {
  if [[ -n "${CDC_EPOCH_PSQL:-}" ]]; then
    eval "$CDC_EPOCH_PSQL"
    return
  fi
  local pod password
  pod="$(kc -n "$DB_NAMESPACE" get pod -l "cnpg.io/cluster=$DB_CLUSTER,cnpg.io/instanceRole=primary" -o name | head -1)"
  [[ -n "$pod" ]] || return 1
  password="$(kc -n "$DB_NAMESPACE" get secret "$DB_CLUSTER-app" -o jsonpath='{.data.password}' | base64 -d)"
  { printf '%s\n' "$password"; cat; } | kc -n "$DB_NAMESPACE" exec -i "$pod" -c postgres -- sh -c \
    'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U shop_app -d shop -v ON_ERROR_STOP=1 -qAt'
}

connect_metrics() {
  if [[ -n "${CDC_EPOCH_METRICS:-}" ]]; then
    eval "$CDC_EPOCH_METRICS"
    return
  fi
  local pod
  pod="$(kc -n "$CONNECT_NAMESPACE" get pod -l "strimzi.io/cluster=$CONNECT_CLUSTER,strimzi.io/kind=KafkaConnect" \
    -o name | head -1)"
  [[ -n "$pod" ]] || return 1
  kc -n "$CONNECT_NAMESPACE" exec "$pod" -- curl -fsS http://localhost:9404/metrics
}

strimzi_crds_ready() { kc get crd kafkatopics.kafka.strimzi.io > /dev/null 2>&1; }
cdc_epochs_table_ready() { run_sql <<< "SELECT count(*) FROM meta.cdc_epochs" > /dev/null 2>&1; }
snapshot_completed() {
  connect_metrics 2> /dev/null | grep '^debezium_metrics_snapshotcompleted{' | grep 'context="snapshot"' \
    | grep "name=\"$DEBEZIUM_SERVER\"" | grep -Eq ' 1(\.0)?$'
}

cmd_new() {
  epoch="${epoch:-$(date +%s)}"
  valid_epoch "$epoch" || die "epoch must be a positive int4, got '$epoch'"
  until_ok "the Strimzi CRDs" strimzi_crds_ready
  kc create namespace "$CONNECT_NAMESPACE" --dry-run=client -o yaml | kc apply -f - > /dev/null
  kc apply -f - > /dev/null << YAML
apiVersion: v1
kind: Secret
metadata:
  name: cdc-epoch
  namespace: $CONNECT_NAMESPACE
  labels:
    app.kubernetes.io/part-of: shopflow
    app.kubernetes.io/managed-by: cdc-epoch
type: Opaque
stringData:
  epoch: "$epoch"
---
apiVersion: kafka.strimzi.io/v1
kind: KafkaTopic
metadata:
  name: iceberg-control-$epoch
  namespace: $CONNECT_NAMESPACE
  labels:
    strimzi.io/cluster: $KAFKA_CLUSTER
    app.kubernetes.io/managed-by: cdc-epoch
spec:
  topicName: iceberg-control-$epoch
  partitions: 1
YAML
  log "epoch $epoch: Secret $CONNECT_NAMESPACE/cdc-epoch and KafkaTopic iceberg-control-$epoch applied ($KUBE_CONTEXT)"
  # Profile rt: the Flink KPI job must start the new epoch from fresh state, never from the previous epoch's
  # checkpoint (docs/adr/0415). Deleting the FlinkDeployment drops its HA state; Argo CD recreates it, and that sync
  # copies the new epoch into namespace flink first.
  if kc get crd flinkdeployments.flink.apache.org > /dev/null 2>&1 \
    && kc -n "$FLINK_NAMESPACE" get flinkdeployment "$FLINK_JOB" > /dev/null 2>&1; then
    kc -n "$FLINK_NAMESPACE" delete flinkdeployment "$FLINK_JOB" --wait=false > /dev/null
    log "epoch $epoch: FlinkDeployment $FLINK_NAMESPACE/$FLINK_JOB deleted, Argo CD recreates it for this epoch"
  fi
  echo "$epoch"
}

cmd_wait() {
  if [[ -z "$epoch" ]]; then
    epoch="$(kc -n "$CONNECT_NAMESPACE" get secret cdc-epoch -o jsonpath='{.data.epoch}' | base64 -d)"
  fi
  valid_epoch "$epoch" || die "epoch must be a positive int4, got '$epoch'"
  until_ok "meta.cdc_epochs (Alembic)" cdc_epochs_table_ready
  run_sql <<< "INSERT INTO meta.cdc_epochs (epoch) VALUES ($epoch) ON CONFLICT (epoch) DO NOTHING" > /dev/null
  log "epoch $epoch: recorded in meta.cdc_epochs, waiting for the Debezium snapshot"
  until_ok "the Debezium snapshot of epoch $epoch" snapshot_completed
  run_sql <<< "UPDATE meta.cdc_epochs SET snapshot_completed_at = now()
    WHERE epoch = $epoch AND snapshot_completed_at IS NULL" > /dev/null
  log "epoch $epoch: snapshot completed, meta.cdc_epochs.snapshot_completed_at set"
}

"cmd_$command"
