#!/usr/bin/env bash
# End-to-end smoke test of the Kafka Connect image on the production data path:
#   shop schema from Alembic (orders image `migrate` + `seed`) -> snapshot, then insert/update/delete
#   -> Debezium -> Kafka -> Iceberg sink -> Polaris + SeaweedFS, read back as the read-only principal.
# The connector configs are the cluster's KafkaConnector CRs (deploy/platform/kafka-connect, local overlay) with only
# the environment-specific keys replaced from overrides/<connector>.json (hosts, credentials, plaintext Kafka).
# Also exercises deploy/platform/iceberg-catalog/base/polaris-setup.py and the bronze DDL.
# Usage: images/kafka-connect/smoke/run.sh   (CONNECT_IMAGE / ORDERS_IMAGE to test other images; KEEP=1 keeps
# the stack). Needs docker compose, kubectl, yq, jq, openssl. Publishes no host ports; removed on exit.
set -euo pipefail

cd "$(dirname "$0")"
REPO="$(cd ../../.. && pwd)"
export CONNECT_IMAGE="${CONNECT_IMAGE:-shopflow-kafka-connect:dev}"
# Orders image with Alembic 0002 (heartbeat, meta.cdc_epochs, shop_cdc, grants) and 0003 (trino_pg grants).
export ORDERS_IMAGE="${ORDERS_IMAGE:-ghcr.io/winthebest/shopflow-orders:sha-ac864b3@sha256:838bf80ce64ff7b45f15744e7e44af5bb88ab673d4e080d2edb7a24b482c1ce1}"
# Same Postgres image as the shop dev compose.
POSTGRES_IMAGE="$(yq '.services.postgres.image' "$REPO/docker-compose.yml")"
export POSTGRES_IMAGE
export CDC_EPOCH="$(date +%s)"
SMOKE_DIR="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/connect-smoke.XXXXXX")" && pwd -P)"
export SMOKE_DIR
mkdir -p "$SMOKE_DIR/initdb" "$SMOKE_DIR/seaweedfs" "$SMOKE_DIR/secrets" "$SMOKE_DIR/connectors"
chmod 0777 "$SMOKE_DIR/secrets" # written by the tools container, read by Connect (uid 1001)
chmod 0755 "$SMOKE_DIR/initdb"

# Throwaway credentials, generated per run and never written to the repository.
secret() { openssl rand -hex 16; }
for name in POSTGRES_PASSWORD SHOP_APP_PASSWORD DEBEZIUM_PASSWORD POLARIS_ROOT_SECRET POLARIS_S3_ACCESS_KEY \
  POLARIS_S3_SECRET_KEY SINK_S3_ACCESS_KEY SINK_S3_SECRET_KEY READER_S3_ACCESS_KEY READER_S3_SECRET_KEY; do
  export "$name=$(secret)"
done

# Roles exactly as the shop dev compose creates them (shop_app owns `shop`; debezium has LOGIN REPLICATION and
# gets its grants from Alembic). Its dev passwords are replaced by this run's random ones before anyone connects.
yq '.configs["init-shop-db"].content' "$REPO/docker-compose.yml" > "$SMOKE_DIR/initdb/10-shop.sql"
# (.sql, not .sh: bind-mounted scripts may look executable to the entrypoint but cannot be run.)
cat > "$SMOKE_DIR/initdb/20-smoke-passwords.sql" <<SQL
ALTER ROLE shop_app PASSWORD '$SHOP_APP_PASSWORD';
ALTER ROLE debezium PASSWORD '$DEBEZIUM_PASSWORD';
SQL
chmod 0644 "$SMOKE_DIR/initdb/"*

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

# Connector configs: cluster CRs + overrides. Fails if a cluster-only provider reference is left behind.
"$REPO/scripts/data-render-overlay.sh" deploy/platform/kafka-connect/local > "$SMOKE_DIR/kafka-connect.yaml"
for connector in shop-postgres iceberg-sink; do
  yq -o=json "select(.kind == \"KafkaConnector\" and .metadata.name == \"$connector\")
      | .spec.config + {\"connector.class\": .spec.class, \"tasks.max\": .spec.tasksMax}" "$SMOKE_DIR/kafka-connect.yaml" \
    | jq --slurpfile o "overrides/$connector.json" \
      '. + $o[0] | with_entries(select(.value != null)) | map_values(tostring)' > "$SMOKE_DIR/connectors/$connector.json"
  if grep -q '\${secrets:' "$SMOKE_DIR/connectors/$connector.json"; then
    echo "$connector: cluster Secret reference without a smoke override" >&2
    exit 1
  fi
done
# Topics as declared by the KafkaTopic CRs, plus this epoch's control topic (created by the epoch tooling).
"$REPO/scripts/data-render-overlay.sh" deploy/platform/kafka/local | yq -N 'select(.kind == "KafkaTopic") | .spec.topicName' \
  > "$SMOKE_DIR/topics.txt"
echo "iceberg-control-$CDC_EPOCH" >> "$SMOKE_DIR/topics.txt"

# Lets `docker compose --env-file` reach this stack after a KEEP=1 run (deleted with the directory).
env | grep -E '^(CONNECT_IMAGE|ORDERS_IMAGE|POSTGRES_IMAGE|CDC_EPOCH|SMOKE_DIR|[A-Z_]+_PASSWORD|(POLARIS|SINK_S3|READER_S3)_[A-Z0-9_]+)=' \
  > "$SMOKE_DIR/.env"
chmod 0600 "$SMOKE_DIR/.env"

cleanup() {
  if [[ "${KEEP:-0}" == "1" ]]; then
    echo "KEEP=1: stack left running; remove with:"
    echo "  docker compose --env-file $SMOKE_DIR/.env -f $PWD/compose.yml --profile '*' down -v && rm -rf $SMOKE_DIR"
    return
  fi
  docker compose --profile '*' down -v --remove-orphans > /dev/null 2>&1 || true
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

psql_shop() { docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d shop -qAt "$@"; }
tools() { docker compose run --rm -T tools "$@"; }

echo "== images: connect $CONNECT_IMAGE, orders $ORDERS_IMAGE; epoch $CDC_EPOCH"
# A stack left by an earlier KEEP=1 run would keep its old database and passwords: start from scratch.
docker compose --profile '*' down -v --remove-orphans > /dev/null 2>&1
docker compose up -d --wait

echo "== shop schema (Alembic) and demo data"
docker compose run --rm -T migrate > /dev/null
docker compose run --rm -T seed > /dev/null
SNAPSHOT_CUSTOMERS="$(psql_shop -c 'SELECT count(*) FROM customers')"
SNAPSHOT_PRODUCTS="$(psql_shop -c 'SELECT count(*) FROM products')"
export SNAPSHOT_CUSTOMERS SNAPSHOT_PRODUCTS
echo "seeded: $SNAPSHOT_CUSTOMERS customers, $SNAPSHOT_PRODUCTS products; publication shop_cdc:" \
  "$(psql_shop -c "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_publication_tables WHERE pubname = 'shop_cdc'")"

echo "== Polaris: catalog, principals, grants (polaris-setup.py), bronze tables (bronze-tables.sql)"
tools python -I /smoke-deploy/polaris-setup.py
tools python -I /smoke/create-bronze-tables.py

echo "== topics (auto-create is off, as on the cluster)"
while read -r topic; do
  # stdin from /dev/null: `exec -T` would otherwise consume the rest of the topic list.
  docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
    --create --if-not-exists --topic "$topic" --partitions 1 --replication-factor 1 < /dev/null > /dev/null
done < "$SMOKE_DIR/topics.txt"
echo "$(wc -l < "$SMOKE_DIR/topics.txt" | tr -d ' ') topics: $(tr '\n' ' ' < "$SMOKE_DIR/topics.txt")"

echo "== connectors (cluster configs + overrides/)"
connect_api PUT /connectors/shop-postgres/config "$SMOKE_DIR/connectors/shop-postgres.json" > /dev/null
wait_running shop-postgres
connect_api PUT /connectors/iceberg-sink/config "$SMOKE_DIR/connectors/iceberg-sink.json" > /dev/null
wait_running iceberg-sink

echo "== changes in Postgres: one checkout, paid, then removed (children before the order: foreign keys)"
psql_shop > /dev/null <<'SQL'
INSERT INTO customers (email, name) VALUES ('smoke@example.test', 'Smoke Test') RETURNING id \gset c_
INSERT INTO orders (customer_id, status, total) VALUES (:c_id, 'pending', 99.90) RETURNING id \gset o_
INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (:o_id, 1, 2, 49.95);
INSERT INTO payments (order_id, amount, status, provider_ref) VALUES (:o_id, 99.90, 'succeeded', 'smoke');
UPDATE orders SET status = 'paid' WHERE id = :o_id;
DELETE FROM payments WHERE order_id = :o_id;
DELETE FROM order_items WHERE order_id = :o_id;
DELETE FROM orders WHERE id = :o_id;
SQL

echo "== Debezium heartbeat topic (topic.heartbeat.prefix=debezium-heartbeat)"
# The console consumer exits 0 on timeout, so require a message on stdout.
heartbeat="$(docker compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic debezium-heartbeat.shop --from-beginning --max-messages 1 --timeout-ms 60000 2> /dev/null)"
[[ -n "$heartbeat" ]] || { echo "no heartbeat event within 60s" >&2; exit 1; }
echo "heartbeat event received"

echo "== bronze, read back as the read-only principal (waits for the sink to commit)"
tools env SNAPSHOT_CUSTOMERS="$SNAPSHOT_CUSTOMERS" SNAPSHOT_PRODUCTS="$SNAPSHOT_PRODUCTS" sh -c '
  pip install --quiet --root-user-action=ignore "pyiceberg[pyarrow]==0.12.0" "pyarrow==25.0.1" \
  && python -I /smoke/verify-bronze.py 180'
