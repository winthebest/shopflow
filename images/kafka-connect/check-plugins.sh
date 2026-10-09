#!/usr/bin/env bash
# Fail unless Kafka Connect inside <image> discovers the Debezium source, its SMT and the Iceberg sink.
# Usage: images/kafka-connect/check-plugins.sh <image>
set -euo pipefail

plugins="$(docker run --rm "$1" /opt/kafka/bin/connect-plugin-path.sh list --plugin-path /opt/kafka/plugins)"
for class in io.debezium.connector.postgresql.PostgresConnector io.debezium.transforms.ExtractNewRecordState \
  org.apache.iceberg.connect.IcebergSinkConnector; do
  grep -F "$class" <<< "$plugins" | cut -f1,4,5 || { echo "plugin $class not found in $1" >&2; exit 1; }
done
