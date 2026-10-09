#!/usr/bin/env bash
# Validate sf-data's GitOps components without a cluster:
#   1. render each component exactly as its Argo CD Application does (chart, version and release name are read
#      from deploy/argocd/apps/<c>/application.yaml; plain manifests through kustomize);
#   2. kubeconform -strict against Kubernetes schemas plus JSON schemas generated from pinned CRDs
#      (Strimzi v1, Argo CD Application, cert-manager Certificate);
#   3. check invariants that must never drift (retention floors on the writable lake catalogs).
# Profile/targetRevision checks belong to scripts/platform-validate.sh (sf-platform) and are not repeated here.
# Components without an app directory are skipped, so the script works while components land in separate PRs.
# Needs: helm, kubectl, kubeconform, yq (mikefarah v4), jq, curl.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPONENTS=(strimzi kafka kafka-connect seaweedfs iceberg-catalog trino)
K8S_VERSION="${K8S_VERSION:-1.34.0}"
SCHEMA_DIR="${SCHEMA_DIR:-out/data-schemas}"
ARGOCD_CRD_VERSION=v3.5.3
CERT_MANAGER_VERSION=v1.21.2
STRIMZI_VERSION="$(yq '.spec.sources[0].targetRevision' deploy/argocd/apps/strimzi/application.yaml 2> /dev/null || echo 1.2.0)"

# CRD (multi-document YAML on stdin) -> $SCHEMA_DIR/<group>/<kind>_<version>.json, the layout kubeconform reads.
# Objects with declared properties are closed (additionalProperties: false) unless the CRD keeps unknown fields:
# the API server would silently prune a misspelled field, so CI has to reject it.
crds_to_schemas() {
  yq -o=json -I=0 'select(.kind == "CustomResourceDefinition") | .spec as $s | .spec.versions[]
    | {"path": ($s.group + "/" + ($s.names.kind | downcase) + "_" + .name + ".json"), "schema": .schema.openAPIV3Schema}' \
    | while IFS= read -r line; do
      path="$SCHEMA_DIR/$(jq -r .path <<< "$line")"
      mkdir -p "$(dirname "$path")"
      jq '.schema | walk(if type == "object" and .type == "object" and has("properties") and (has("additionalProperties") | not)
        and ((.["x-kubernetes-preserve-unknown-fields"] // false) | not)
        then . + {additionalProperties: false} else . end)' <<< "$line" > "$path"
    done
}

generate_schemas() {
  local stamp="strict-v2 strimzi=$STRIMZI_VERSION argocd=$ARGOCD_CRD_VERSION cert-manager=$CERT_MANAGER_VERSION"
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
  echo "$stamp" > "$SCHEMA_DIR/.stamp"
}

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
    helm template "$release" "${ref[@]}" --version "$version" --namespace "$namespace" \
      -f "deploy/platform/$c/base/values.yaml" -f "deploy/platform/$c/local/values.yaml"
  fi
  if [[ -f "deploy/platform/$c/local/kustomization.yaml" ]]; then
    echo "---"
    kubectl kustomize "deploy/platform/$c/local"
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

generate_schemas
validated=0
for c in "${COMPONENTS[@]}"; do
  [[ -d "deploy/argocd/apps/$c" ]] || continue
  echo "== $c"
  kubectl kustomize "deploy/argocd/apps/$c" | kubeconform_strict
  rendered="$(render "$c")"
  kubeconform_strict <<< "$rendered"
  [[ "$c" == trino ]] && check_retention_floors "$rendered"
  validated=$((validated + 1))
done
echo "validated $validated data component(s)"
