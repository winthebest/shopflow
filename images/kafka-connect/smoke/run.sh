#!/usr/bin/env bash
# End-to-end smoke test of the Kafka Connect image on the production data path:
#   snapshot + insert/update/delete in Postgres -> Debezium (SMTs, heartbeat query, epoch) -> Kafka
#   -> Iceberg sink (per-epoch control topic, static routing, no auto-create) -> Polaris + SeaweedFS.
# Also exercises deploy/platform/iceberg-catalog/base/polaris-setup.py and the bronze DDL.
# Usage: images/kafka-connect/smoke/run.sh   (CONNECT_IMAGE=<ref> to test another image; KEEP=1 to keep the stack)
# Needs docker compose and openssl. Publishes no host ports; everything is removed on exit unless KEEP=1.
set -euo pipefail

cd "$(dirname "$0")"
export CONNECT_IMAGE="${CONNECT_IMAGE:-shopflow-kafka-connect:dev}"
export CDC_EPOCH="$(date +%s)"
SMOKE_DIR="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/connect-smoke.XXXXXX")" && pwd -P)"
export SMOKE_DIR
mkdir -p "$SMOKE_DIR/seaweedfs" "$SMOKE_DIR/secrets"
chmod 0777 "$SMOKE_DIR/secrets" # written by the tools container, read by Connect (uid 1001)

# Throwaway credentials, generated per run and never written to the repository.
secret() { openssl rand -hex 16; }
for name in POSTGRES_PASSWORD DEBEZIUM_PASSWORD POLARIS_ROOT_SECRET POLARIS_S3_ACCESS_KEY POLARIS_S3_SECRET_KEY \
  SINK_S3_ACCESS_KEY SINK_S3_SECRET_KEY READER_S3_ACCESS_KEY READER_S3_SECRET_KEY; do
  export "$name=$(secret)"
done

# SeaweedFS identities, same split as deploy/platform/seaweedfs: writers vs. a read-only reader.
cat > "$SMOKE_DIR/seaweedfs/s3.json" <<JSON
{"identities": [
  {"name": "polaris", "credentials": [{"accessKey": "$POLARIS_S3_ACCESS_KEY", "secretKey": "$POLARIS_S3_SECRET_KEY"}],
   "actions": ["Read:lake", "List:lake", "Write:lake"]},
  {"name": "iceberg-sink", "credentials": [{"accessKey": "$SINK_S3_ACCESS_KEY", "secretKey": "$SINK_S3_SECRET_KEY"}],
   "actions": ["Read:lake", "List:lake", "Write:lake"]},
  {"name": "trino-lake-ro", "credentials": [{"accessKey": "$READER_S3_ACCESS_KEY", "secretKey": "$READER_S3_SECRET_KEY"}],
   "actions": ["Read:lake", "List:lake"]}
]}
JSON
chmod 0644 "$SMOKE_DIR/seaweedfs/s3.json"
# Lets `docker compose --env-file` reach this stack after a KEEP=1 run (deleted with the directory).
env | grep -E '^(CONNECT_IMAGE|CDC_EPOCH|SMOKE_DIR|POSTGRES_PASSWORD|DEBEZIUM_PASSWORD|POLARIS_|SINK_S3_|READER_S3_)' \
  > "$SMOKE_DIR/.env"
chmod 0600 "$SMOKE_DIR/.env"

cleanup() {
  if [[ "${KEEP:-0}" == "1" ]]; then
    echo "KEEP=1: stack left running; remove with:"
    echo "  docker compose --env-file $SMOKE_DIR/.env -f $PWD/compose.yml down -v && rm -rf $SMOKE_DIR"
    return
  fi
  docker compose down -v --remove-orphans > /dev/null 2>&1 || true
  rm -rf "$SMOKE_DIR"
}
trap cleanup EXIT

connect_api() { # method path [json-file]
  if [[ $# -eq 3 ]]; then
    docker compose exec -T connect curl -fsS -X "$1" -H 'Content-Type: application/json' \
      --data-binary @- "http://localhost:8083$2" < "$3"
  else
    docker compose exec -T connect curl -fsS -X "$1" "http://localhost:8083$2"
  fi
}

wait_running() { # connector
  for _ in $(seq 60); do
    if connect_api GET "/connectors/$1/status" 2> /dev/null | grep -q '"tasks":\[{"id":0,"state":"RUNNING"'; then
      echo "connector $1: RUNNING"
      return
    fi
    sleep 2
  done
  connect_api GET "/connectors/$1/status" || true
  echo "connector $1 did not reach RUNNING" >&2
  return 1
}

psql_shop() { docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d shop -qc "$1"; }
tools() { docker compose run --rm -T tools "$@"; }

echo "== image $CONNECT_IMAGE, epoch $CDC_EPOCH"
docker compose up -d --wait

echo "== Polaris: catalog, principals, grants (polaris-setup.py), bronze tables (bronze-tables.sql)"
tools python -I /smoke-deploy/polaris-setup.py
tools python -I /smoke/create-bronze-tables.py customers orders heartbeat

echo "== topics (auto-create is off, as on the cluster)"
for topic in shop.public.customers shop.public.orders shop.public.heartbeat __debezium-heartbeat.shop \
  "iceberg-control-$CDC_EPOCH"; do
  docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
    --create --if-not-exists --topic "$topic" --partitions 1 --replication-factor 1 > /dev/null
done

echo "== plugins"
plugins="$(connect_api GET /connector-plugins)"
for class in io.debezium.connector.postgresql.PostgresConnector org.apache.iceberg.connect.IcebergSinkConnector; do
  grep -q "\"$class\"" <<< "$plugins" || { echo "plugin $class not found" >&2; exit 1; }
  echo "found $class"
done

echo "== connectors"
connect_api PUT /connectors/shop-postgres/config connectors/debezium-postgres.json > /dev/null
wait_running shop-postgres
connect_api PUT /connectors/iceberg-sink/config connectors/iceberg-sink.json > /dev/null
wait_running iceberg-sink

echo "== changes in Postgres"
psql_shop "INSERT INTO customers (email, name) VALUES ('c@example.test', 'Carol')"
psql_shop "INSERT INTO orders (customer_id, status, total) VALUES (3, 'pending', 99.90)"
psql_shop "UPDATE orders SET status = 'paid' WHERE id = 2"
psql_shop "DELETE FROM orders WHERE id = 1"

echo "== bronze, read back as the read-only principal (waits for the sink to commit)"
tools sh -c 'pip install --quiet --root-user-action=ignore "pyiceberg[pyarrow]==0.12.0" "pyarrow==25.0.1" \
  && python -I /smoke/verify-bronze.py 180'
