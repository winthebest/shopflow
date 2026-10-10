#!/usr/bin/env bash
# Validate sf-data's GitOps components without a cluster:
#   1. render each component exactly as its Argo CD Application does (chart, version and release name are read
#      from deploy/argocd/apps/<c>/application.yaml; plain manifests through kustomize);
#   2. kubeconform -strict against Kubernetes schemas plus JSON schemas generated from pinned CRDs
#      (Strimzi v1, Argo CD Application, cert-manager Certificate);
#   3. check invariants that must never drift: retention floors on the writable lake catalogs, and the CDC table
#      list (data/contracts = Debezium include list = Kafka topics = sink topics/tables/routes = bronze DDL, with
#      every contract column present in bronze with a compatible type: the sink silently drops unknown columns).
# Profile/targetRevision checks belong to scripts/platform-validate.sh (sf-platform) and are not repeated here.
# Components without an app directory are skipped, so the script works while components land in separate PRs.
# Needs: helm, kubectl, kubeconform, yq (mikefarah v4), jq, curl.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPONENTS=(strimzi kafka kafka-connect seaweedfs iceberg-catalog trino freshness-exporter airflow flink-operator flink)
K8S_VERSION="${K8S_VERSION:-1.34.0}"
SCHEMA_DIR="${SCHEMA_DIR:-out/data-schemas}"
ARGOCD_CRD_VERSION=v3.5.3
CERT_MANAGER_VERSION=v1.21.2
STRIMZI_VERSION="$(yq '.spec.sources[0].targetRevision' deploy/argocd/apps/strimzi/application.yaml 2> /dev/null || echo 1.2.0)"
# PodMonitor / ServiceMonitor schemas come from the kube-prometheus-stack chart that sf-sre deploys.
KPS_VERSION="$(yq '.spec.sources[0].targetRevision' deploy/argocd/apps/kube-prometheus-stack/application.yaml)"
# FlinkDeployment schemas come from the operator chart the flink-operator app pins.
FLINK_OPERATOR_REPO="$(yq '.spec.sources[0].repoURL' deploy/argocd/apps/flink-operator/application.yaml)"
FLINK_OPERATOR_VERSION="$(yq '.spec.sources[0].targetRevision' deploy/argocd/apps/flink-operator/application.yaml)"

# CRD (multi-document YAML on stdin) -> $SCHEMA_DIR/<group>/<kind>_<version>.json, the layout kubeconform reads.
# Objects with declared properties are closed (additionalProperties: false) unless the CRD keeps unknown fields:
# the API server would silently prune a misspelled field, so CI has to reject it.
crds_to_schemas() {
  # shellcheck disable=SC2016 # $s is a yq variable, not a shell expansion
  yq -o=json -I=0 'select(.kind == "CustomResourceDefinition") | .spec as $s | .spec.versions[]
    | {"path": ($s.group + "/" + ($s.names.kind | downcase) + "_" + .name + ".json"), "schema": .schema.openAPIV3Schema}' \
    | while IFS= read -r line; do
      path="$SCHEMA_DIR/$(jq -r .path <<< "$line")"
      mkdir -p "$(dirname "$path")"
      # Some CRDs (Flink's) omit the root apiVersion/kind/metadata; add them so closing the root keeps them valid.
      jq '.schema | .properties.apiVersion //= {type: "string"} | .properties.kind //= {type: "string"}
        | .properties.metadata //= {type: "object"}
        | walk(if type == "object" and .type == "object" and has("properties") and (has("additionalProperties") | not)
        and ((.["x-kubernetes-preserve-unknown-fields"] // false) | not)
        then . + {additionalProperties: false} else . end)' <<< "$line" > "$path"
    done
}

generate_schemas() {
  local stamp="strict-v3 strimzi=$STRIMZI_VERSION argocd=$ARGOCD_CRD_VERSION cert-manager=$CERT_MANAGER_VERSION"
  stamp+=" kube-prometheus-stack=$KPS_VERSION flink-operator=$FLINK_OPERATOR_VERSION"
  if [[ -f "$SCHEMA_DIR/.stamp" && "$(cat "$SCHEMA_DIR/.stamp")" == "$stamp" ]]; then
    return
  fi
  rm -rf "$SCHEMA_DIR" && mkdir -p "$SCHEMA_DIR"
  echo "== generating CRD schemas ($stamp)"
  helm show crds oci://quay.io/strimzi-helm/strimzi-kafka-operator --version "$STRIMZI_VERSION" 2> /dev/null | crds_to_schemas
  curl -fsSL "https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_CRD_VERSION/manifests/crds/application-crd.yaml" \
    | crds_to_schemas
  curl -fsSL "https://github.com/cert-manager/cert-manager/releases/download/$CERT_MANAGER_VERSION/cert-manager.crds.yaml" \
    | crds_to_schemas
  helm show crds kube-prometheus-stack --repo https://prometheus-community.github.io/helm-charts \
    --version "$KPS_VERSION" 2> /dev/null | crds_to_schemas
  helm show crds flink-kubernetes-operator --repo "$FLINK_OPERATOR_REPO" --version "$FLINK_OPERATOR_VERSION" \
    2> /dev/null | crds_to_schemas
  echo "$stamp" > "$SCHEMA_DIR/.stamp"
}

RAW_REPO_PREFIX=https://raw.githubusercontent.com/winthebest/shopflow/

# Print the manifests Argo CD would apply for one component (local overlay).
render() {
  local c="$1" app="deploy/argocd/apps/$1/application.yaml"
  local chart repo version release namespace
  chart="$(yq '.spec.sources[0].chart // ""' "$app")"
  if [[ -n "$chart" ]]; then
    repo="$(yq '.spec.sources[0].repoURL' "$app")"
    version="$(yq '.spec.sources[0].targetRevision' "$app")"
    release="$(yq '.spec.sources[0].helm.releaseName' "$app")"
    namespace="$(yq '.spec.destination.namespace' "$app")"
    local ref=("oci://$repo/$chart") # Argo CD treats a repoURL without scheme as an OCI registry
    [[ "$repo" == *://* ]] && ref=(--repo "$repo" "$chart")
    # A Helm index kept in this repo (raw URL of a branch): read it from the checkout, so a PR is validated with its
    # own index, and render the tarball it points to.
    if [[ "$repo" == "$RAW_REPO_PREFIX"* ]]; then
      local index="${repo#"$RAW_REPO_PREFIX"}"
      index="${index#*/}/index.yaml"
      ref=("$(yq ".entries.${chart}[] | select(.version == \"$version\") | .urls[0]" "$index")")
    fi
    helm template "$release" "${ref[@]}" --version "$version" --namespace "$namespace" \
      -f "deploy/platform/$c/base/values.yaml" -f "deploy/platform/$c/local/values.yaml"
  fi
  if [[ -f "deploy/platform/$c/local/kustomization.yaml" ]]; then
    echo "---"
    scripts/data-render-overlay.sh "deploy/platform/$c/local"
  fi
}

kubeconform_strict() {
  kubeconform -strict -summary -kubernetes-version "$K8S_VERSION" \
    -schema-location default \
    -schema-location "$SCHEMA_DIR/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" -
}

# Retention floors on catalogs that can run table procedures: a hand-set 7d constant, never lower, never 0s.
check_retention_floors() {
  local rendered="$1" catalog props key
  for catalog in lake lake_ro; do
    props="$(yq "select(.kind == \"ConfigMap\" and .metadata.name == \"trino-catalog\") | .data[\"$catalog.properties\"]" <<< "$rendered")"
    for key in iceberg.expire-snapshots.min-retention iceberg.remove-orphan-files.min-retention; do
      if ! grep -qx "$key=7d" <<< "$props"; then
        echo "FAIL trino catalog $catalog: expected $key=7d" >&2
        return 1
      fi
    done
  done
  echo "retention floors: lake, lake_ro = 7d"
}

# Fail unless the two newline-separated lists are equal (both sorted by the caller).
same_tables() { # label expected actual
  if [[ "$2" != "$3" ]]; then
    echo "FAIL $1 does not match data/contracts:" >&2
    diff <(echo "$2") <(echo "$3") >&2 || true
    return 1
  fi
}

# Bronze column type for a Postgres contract type (Iceberg has no smallint; timestamps keep microseconds).
bronze_type() {
  case "$1" in
    bigint | integer | boolean | date) echo "$1" ;;
    smallint) echo integer ;;
    text) echo varchar ;;
    "timestamp with time zone") echo "timestamp(6) with time zone" ;;
    numeric\(*) sed -E 's/numeric\(([0-9]+), *([0-9]+)\)/decimal(\1, \2)/' <<< "$1" ;;
    *) echo "unmapped:$1" ;;
  esac
}

check_cdc_contracts() { # rendered-kafka rendered-kafka-connect
  local ddl=deploy/platform/trino/base/bronze-tables.sql contracts connector f table name type want got
  contracts="$(for f in data/contracts/*.yaml; do yq '.table' "$f"; done | sort)"
  same_tables "Debezium table.include.list" "$contracts" "$(yq 'select(.kind == "KafkaConnector" and .metadata.name == "shop-postgres")
    | .spec.config["table.include.list"]' <<< "$2" | tr ',' '\n' | sed 's/^public\.//' | sort)"
  connector='select(.kind == "KafkaConnector" and .metadata.name == "iceberg-sink") | .spec.config'
  same_tables "sink topics" "$contracts" "$(yq "$connector | .topics" <<< "$2" | tr ',' '\n' | sed 's/^shop\.public\.//' | sort)"
  same_tables "sink iceberg.tables" "$contracts" \
    "$(yq "$connector | .[\"iceberg.tables\"]" <<< "$2" | tr ',' '\n' | sed 's/^bronze\.//' | sort)"
  same_tables "sink route-regex" "$contracts" "$(yq "$connector | keys | .[]" <<< "$2" \
    | sed -nE 's/^iceberg\.table\.bronze\.([a-z_]+)\.route-regex$/\1/p' | sort)"
  same_tables "KafkaTopics shop.public.*" "$contracts" "$(yq 'select(.kind == "KafkaTopic") | .spec.topicName' <<< "$1" \
    | sed -nE 's/^shop\.public\.//p' | sort)"
  same_tables "bronze DDL" "$contracts" "$(sed -nE 's/^CREATE TABLE IF NOT EXISTS lake\.bronze\.([a-z_]+) \($/\1/p' "$ddl" | sort)"
  for f in data/contracts/*.yaml; do
    table="$(yq '.table' "$f")"
    while IFS=$'\t' read -r name type; do
      want="$(bronze_type "$type")"
      got="$(awk -v t="$table" -v c="$name" '
        $0 == "CREATE TABLE IF NOT EXISTS lake.bronze." t " (" { inside = 1; next }
        inside && /^\)/ { exit }
        inside && $1 == c { $1 = ""; sub(/^ +/, ""); sub(/,$/, ""); print; exit }' "$ddl")"
      if [[ "$got" != "$want" ]]; then
        echo "FAIL bronze.$table.$name: contract type '$type' needs '$want' in $ddl, found '${got:-missing}'" >&2
        return 1
      fi
    done < <(yq '.columns[] | [.name, .type] | @tsv' "$f")
  done
  echo "CDC tables: data/contracts = Debezium = topics = sink = bronze DDL ($(echo "$contracts" | wc -l | tr -d ' ') tables, columns typed)"
}

# Secrets: every value in an sf-data *.enc.yaml is SOPS-encrypted (nothing in plaintext in git), and the in-cluster
# copy script is identical in every component that ships it.
check_secrets() {
  local f plain
  while IFS= read -r f; do
    plain="$(yq '[(.stringData // {}), (.data // {})] | .[] | to_entries | .[] | select(.value | test("^ENC\\[") | not) | .key' "$f")"
    if [[ -n "$plain" ]]; then
      echo "FAIL $f: unencrypted keys: $plain" >&2
      return 1
    fi
  done < <(find deploy/platform/{strimzi,kafka,kafka-connect,seaweedfs,iceberg-catalog,trino,airflow,flink} -name '*.enc.yaml' 2> /dev/null)
  local copy
  # Helm charts cannot read files outside the chart, so a chart keeps its own copy under files/.
  for copy in deploy/platform/*/*/copy-secret.py deploy/charts/*/files/copy-secret.py; do
    [[ -e "$copy" ]] || continue
    if ! cmp -s "$copy" deploy/platform/trino/base/copy-secret.py; then
      echo "FAIL $copy differs from deploy/platform/trino/base/copy-secret.py" >&2
      return 1
    fi
  done
  echo "secrets: all sf-data *.enc.yaml values encrypted; copy-secret.py identical"
}

# `cdc-epoch.sh new` against a fake kubectl: it must select the context from CLUSTER and apply a Namespace, the
# cdc-epoch Secret and the per-epoch KafkaTopic that pass kubeconform -strict (no cluster in CI).
check_cdc_epoch_new() {
  local fake
  fake="$(mktemp -d)"
  cat > "$fake/kubectl" << 'SH'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_DIR/calls.log"
case "$*" in
  *"create namespace"*) printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: kafka\n' ;;
  *"apply -f -"*) { cat; echo "---"; } >> "$FAKE_DIR/applied.yaml" ;;
esac
SH
  chmod +x "$fake/kubectl"
  FAKE_DIR="$fake" PATH="$fake:$PATH" CLUSTER=sf-ci scripts/cdc-epoch.sh new --epoch 1234567890 --timeout 5 > "$fake/out"
  [[ "$(cat "$fake/out")" == 1234567890 ]] || { echo "FAIL cdc-epoch.sh new did not print the epoch" >&2; return 1; }
  grep -q -- '--context k3d-sf-ci apply' "$fake/calls.log" || { echo "FAIL cdc-epoch.sh new ignored CLUSTER" >&2; return 1; }
  [[ "$(yq -N 'select(.kind == "KafkaTopic") | .spec.topicName' "$fake/applied.yaml")" == iceberg-control-1234567890 ]] \
    || { echo "FAIL cdc-epoch.sh new: no KafkaTopic iceberg-control-<epoch>" >&2; return 1; }
  # The fake answers every `get`, so the Flink job of profile rt exists: a new epoch must delete it.
  grep -q -- '-n flink delete flinkdeployment kpi-minute' "$fake/calls.log" \
    || { echo "FAIL cdc-epoch.sh new did not delete FlinkDeployment flink/kpi-minute" >&2; return 1; }
  kubeconform_strict < "$fake/applied.yaml"
  rm -rf "$fake"
  echo "cdc-epoch.sh new: Secret + KafkaTopic valid (fake kubectl)"
}

# `cdc-epoch.sh ensure` (make up) keeps the epoch of a running cluster and starts one only without the Secret; `wait`
# must not record a snapshot from Debezium's metric alone while bronze has no snapshot rows of the epoch.
check_cdc_epoch_ensure_wait() {
  local fake out
  fake="$(mktemp -d)"
  cat > "$fake/kubectl" << 'SH'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_DIR/calls.log"
case "$*" in
  *"get secret cdc-epoch --ignore-not-found"*) [[ -z "${FAKE_EPOCH:-}" ]] || printf '%s' "$FAKE_EPOCH" | base64 ;;
  *"create namespace"*) printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: kafka\n' ;;
  *"apply -f -"*) cat > /dev/null ;;
esac
SH
  chmod +x "$fake/kubectl"
  local run=(env FAKE_DIR="$fake" PATH="$fake:$PATH" CLUSTER=sf-ci scripts/cdc-epoch.sh)
  # Re-running make up: the Secret holds an epoch, so ensure prints it and changes nothing.
  out="$(FAKE_EPOCH=1111111111 "${run[@]}" ensure --timeout 5)"
  [[ "$out" == 1111111111 ]] || { echo "FAIL cdc-epoch.sh ensure did not keep the current epoch (got '$out')" >&2; return 1; }
  if grep -qE ' (apply|delete) ' "$fake/calls.log"; then
    echo "FAIL cdc-epoch.sh ensure changed a cluster that already has an epoch" >&2
    return 1
  fi
  # A fresh cluster: no Secret, so ensure starts the epoch as new does.
  : > "$fake/calls.log"
  out="$("${run[@]}" ensure --epoch 1222222222 --timeout 5)"
  if ! { [[ "$out" == 1222222222 ]] && grep -q 'apply -f -' "$fake/calls.log"; }; then
    echo "FAIL cdc-epoch.sh ensure did not start an epoch on a fresh cluster" >&2
    return 1
  fi
  # wait: Debezium reports a completed snapshot (a task older than the epoch) but bronze has no rows of it, so wait
  # must time out without setting snapshot_completed_at; with bronze rows it records the snapshot.
  # shellcheck disable=SC2016 # expanded by cdc-epoch.sh when it runs the seam commands
  local seams=(CDC_EPOCH_PSQL='cat >> "$FAKE_DIR/sql.log"; echo 1'
    CDC_EPOCH_METRICS='echo "debezium_metrics_snapshotcompleted{context=\"snapshot\",name=\"shop\",plugin=\"postgres\"} 1.0"')
  if env "${seams[@]}" CDC_EPOCH_BRONZE='echo 0' "${run[@]}" wait --epoch 1333333333 --timeout 6 2> /dev/null; then
    echo "FAIL cdc-epoch.sh wait accepted the Debezium metric without bronze rows of the epoch" >&2
    return 1
  fi
  if grep -q 'SET snapshot_completed_at' "$fake/sql.log"; then
    echo "FAIL cdc-epoch.sh wait set snapshot_completed_at for an epoch without bronze rows" >&2
    return 1
  fi
  if ! { env "${seams[@]}" CDC_EPOCH_BRONZE='echo 1' "${run[@]}" wait --epoch 1333333333 --timeout 30 2> /dev/null \
    && grep -q 'SET snapshot_completed_at' "$fake/sql.log"; }; then
    echo "FAIL cdc-epoch.sh wait did not record a snapshot that bronze shows" >&2
    return 1
  fi
  # wait on an epoch already recorded (make up re-run after a Connect restart: metric 0, no new snapshot) returns at
  # once and changes nothing.
  : > "$fake/sql.log"
  # shellcheck disable=SC2016 # expanded by cdc-epoch.sh when it runs the seam commands
  if ! env CDC_EPOCH_PSQL='tee -a "$FAKE_DIR/sql.log" | grep -q "snapshot_completed_at IS NOT NULL" \
      && echo "completed 2026-10-10 12:11:29+00" || echo 1' \
    CDC_EPOCH_METRICS='echo "debezium_metrics_snapshotcompleted{context=\"snapshot\",name=\"shop\"} 0.0"' \
    CDC_EPOCH_BRONZE='echo 0' "${run[@]}" wait --epoch 1333333333 --timeout 6 2> /dev/null; then
    echo "FAIL cdc-epoch.sh wait did not return for an epoch already recorded" >&2
    return 1
  fi
  if grep -qE 'INSERT|UPDATE' "$fake/sql.log"; then
    echo "FAIL cdc-epoch.sh wait wrote to meta.cdc_epochs for an epoch already recorded" >&2
    return 1
  fi
  # Timeout: a new epoch whose snapshot never completes (metric 0, no bronze rows) makes wait fail, so make up fails
  # loudly instead of leaving silver and gold empty without notice.
  # shellcheck disable=SC2016 # expanded by cdc-epoch.sh when it runs the seam commands
  if env CDC_EPOCH_PSQL='cat >> "$FAKE_DIR/sql.log"; echo 1' \
    CDC_EPOCH_METRICS='echo "debezium_metrics_snapshotcompleted{context=\"snapshot\",name=\"shop\"} 0.0"' \
    CDC_EPOCH_BRONZE='echo 0' "${run[@]}" wait --epoch 1444444444 --timeout 6 2> /dev/null; then
    echo "FAIL cdc-epoch.sh wait succeeded for a snapshot that never completed" >&2
    return 1
  fi
  rm -rf "$fake"
  echo "cdc-epoch.sh ensure keeps a running cluster's epoch; wait needs bronze rows, skips a completed epoch, times out otherwise (fake kubectl)"
}

# `data-secrets.sh --aws-json` (input of scripts/aws-seed-params.sh): the Trino Secrets with the names and keys of the
# local SOPS files, the Trino user group complete, and password.db holding bcrypt hashes of exactly those passwords.
# Values stay in this process: never printed, never on a command line.
check_aws_json() {
  local json dir name
  json="$(scripts/data-secrets.sh --aws-json)"
  jq -e '(keys == ["_groups", "lakehouse"])
    and (._groups == [["lakehouse/trino-dbt", "lakehouse/trino-exporter", "lakehouse/trino-password-db"]])
    and ([.lakehouse[][] | select(type != "string" or length == 0)] == [])' <<< "$json" > /dev/null \
    || { echo "FAIL data-secrets.sh --aws-json: unexpected shape, group or empty value" >&2; return 1; }
  for name in $(jq -r '.lakehouse | keys[]' <<< "$json"); do
    [[ "$(jq -c --arg n "$name" '.lakehouse[$n] | keys' <<< "$json")" \
      == "$(yq -o=json -I=0 '.stringData | keys | sort' "deploy/platform/trino/local/secrets/$name.enc.yaml")" ]] \
      || { echo "FAIL --aws-json $name: keys differ from deploy/platform/trino/local/secrets/$name.enc.yaml" >&2; return 1; }
  done
  dir="$(mktemp -d)"
  jq -r '.lakehouse["trino-password-db"]["password.db"]' <<< "$json" > "$dir/password.db"
  for name in dbt exporter; do
    jq -r --arg n "trino-$name" '.lakehouse[$n].password' <<< "$json" \
      | htpasswd -vi "$dir/password.db" "$name" 2> /dev/null \
      || { rm -rf "$dir"; echo "FAIL --aws-json: password.db does not match trino-$name" >&2; return 1; }
  done
  rm -rf "$dir"
  echo "data-secrets.sh --aws-json: Trino group complete, keys match the SOPS files, bcrypt matches"
}

generate_schemas
check_secrets
check_cdc_epoch_new
check_cdc_epoch_ensure_wait
check_aws_json
RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT
validated=0
for c in "${COMPONENTS[@]}"; do
  [[ -d "deploy/argocd/apps/$c" ]] || continue
  echo "== $c"
  kubectl kustomize "deploy/argocd/apps/$c" | kubeconform_strict
  render "$c" > "$RENDER_DIR/$c.yaml"
  kubeconform_strict < "$RENDER_DIR/$c.yaml"
  [[ "$c" == trino ]] && check_retention_floors "$(cat "$RENDER_DIR/trino.yaml")"
  validated=$((validated + 1))
done
if [[ -f "$RENDER_DIR/kafka.yaml" && -f "$RENDER_DIR/kafka-connect.yaml" ]]; then
  check_cdc_contracts "$(cat "$RENDER_DIR/kafka.yaml")" "$(cat "$RENDER_DIR/kafka-connect.yaml")"
fi
echo "validated $validated data component(s)"
