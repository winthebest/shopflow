#!/usr/bin/env bash
# Offline checks for the AWS-only components sf-cloud owns (external-secrets, aws-lb-controller, opencost).
# platform-validate.sh already renders every app, runs kubeconform with the public CRD catalog and checks image
# digests; this adds what it cannot know:
#   1. External Secrets custom resources validated against the CRDs shipped in the pinned chart (the public
#      catalog has no schema for external-secrets.io/v1, so they would only be "skipped");
#   2. the manifests agree with infra/cloud-contract.json: Pod Identity service accounts, ESO namespaces,
#      role prefix, SSM prefix and region, the CDC epoch parameter, the LB controller's cost/reaper tags.
# Needs: helm, yq, jq, kubeconform. Reads chart repositories over the network; never touches AWS.
# shellcheck disable=SC2016 # single-quoted strings are yq/jq programs, not shell
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

KUBERNETES_VERSION="${KUBERNETES_VERSION:-1.34.12}"
APPS=(external-secrets aws-lb-controller opencost)

usage() {
  cat <<'EOF'
Usage: scripts/cloud-manifests-check.sh
EOF
}

fail() { die "$*"; }

# Render one Application's chart and path sources, as Argo CD would (no session parameters: placeholders).
render_app() {
  local app_file="$1" count i chart repo ns release dir
  ns="$(yq '.spec.destination.namespace' "$app_file")"
  count="$(yq '.spec.sources | length' "$app_file")"
  for ((i = 0; i < count; i++)); do
    chart="$(yq ".spec.sources[$i].chart // \"\"" "$app_file")"
    release="$(yq ".spec.sources[$i].helm.releaseName // \"x\"" "$app_file")"
    if [ -n "$chart" ]; then
      repo="$(yq ".spec.sources[$i].repoURL" "$app_file")"
      local values=()
      while IFS= read -r vf; do
        [ -n "$vf" ] && values+=(--values "$REPO_ROOT/${vf#\$values/}")
      done < <(yq ".spec.sources[$i].helm.valueFiles[]?" "$app_file")
      helm template "$release" "$chart" --repo "$repo" --version "$(yq ".spec.sources[$i].targetRevision" "$app_file")" \
        --namespace "$ns" --kube-version "$KUBERNETES_VERSION" --include-crds "${values[@]}"
    elif [ "$(yq ".spec.sources[$i].path // \"\"" "$app_file")" != "" ]; then
      dir="$REPO_ROOT/$(yq ".spec.sources[$i].path" "$app_file")"
      helm template "$release" "$dir" --namespace "$ns" --kube-version "$KUBERNETES_VERSION"
    fi
    echo "---"
  done
}

# kubeconform schemas (strict: unknown fields rejected) from every CRD version in a rendered file.
crd_schemas() {
  local rendered="$1" out="$2"
  mkdir -p "$out"
  yq -o=json -I=0 'select(.kind == "CustomResourceDefinition")' "$rendered" | while IFS= read -r crd; do
    jq -c '.spec.names.kind as $kind | .spec.versions[] | {kind: $kind, version: .name, schema: .schema.openAPIV3Schema}' <<<"$crd"
  done | while IFS= read -r version; do
    local kind ver
    kind="$(jq -r '.kind | ascii_downcase' <<<"$version")"
    ver="$(jq -r '.version' <<<"$version")"
    # Strict like openapi2jsonschema: closed objects reject unknown fields. Only real object schemas
    # (`type: object`) are closed, never a `properties` map that happens to contain a field named "properties".
    jq '.schema | walk(if type == "object" and .type == "object" and has("properties")
                         and (has("additionalProperties") | not) and (has("x-kubernetes-preserve-unknown-fields") | not)
                       then . + {additionalProperties: false} else . end)' <<<"$version" >"$out/${kind}_${ver}.json"
  done
}

expect() { # expect <description> <actual> <expected>
  [ "$2" = "$3" ] || fail "$1: got '$2', want '$3'"
  log "ok: $1"
}

main() {
  parse_common_args "$@"
  require_cmds helm yq jq kubeconform
  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "$WORK_DIR"' EXIT
  local work="$WORK_DIR"

  local app
  for app in "${APPS[@]}"; do
    render_app "$REPO_ROOT/deploy/argocd/apps/$app/application.yaml" >"$work/$app.yaml" || fail "$app does not render"
    log "rendered $app"
  done

  # 1. External Secrets CRs against the CRDs of the pinned chart.
  crd_schemas "$work/external-secrets.yaml" "$work/schemas"
  yq 'select(.apiVersion == "external-secrets.io/*")' "$work/external-secrets.yaml" >"$work/eso-crs.yaml"
  kubeconform -strict -summary -kubernetes-version "$KUBERNETES_VERSION" \
    -schema-location "$work/schemas/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" "$work/eso-crs.yaml" ||
    fail "External Secrets resources do not match the chart's CRDs"

  # 2. Contract.
  local stores_values="$REPO_ROOT/deploy/platform/external-secrets/aws/secret-stores/values.yaml"
  expect "ESO namespaces = eso_namespaces" "$(yq -o=json -I=0 '.namespaces' "$stores_values")" "$(jq -c .eso_namespaces "$CONTRACT")"
  expect "ESO role prefix" "$(yq '.rolePrefix' "$stores_values")" "$(contract .roles.eso_prefix)"
  expect "SSM prefix" "$(yq '.parameterPrefix' "$stores_values")" "$SSM_PREFIX"
  expect "ESO region" "$(yq '.region' "$stores_values")" "$REGION"
  expect "one ClusterSecretStore per namespace" \
    "$(yq -N 'select(.kind == "ClusterSecretStore") | .spec.conditions[0].namespaces[0]' "$work/external-secrets.yaml" | sort | tr '\n' ' ')" \
    "$(jq -r '.eso_namespaces[]' "$CONTRACT" | sort | tr '\n' ' ')"
  expect "CDC epoch parameter" \
    "$(yq -N 'select(.kind == "ExternalSecret" and .metadata.name == "cdc-epoch") | .spec.data[0].remoteRef.key' "$work/external-secrets.yaml")" \
    "$SSM_CDC_EPOCH"
  local ns key
  while read -r ns key; do
    case "$key" in "$SSM_PREFIX/$ns/"*) ;; *) fail "ExternalSecret in $ns reads $key, outside $SSM_PREFIX/$ns/" ;; esac
  done < <(yq -N 'select(.kind == "ExternalSecret") | .metadata.namespace as $ns | .spec.data[] | $ns + " " + .remoteRef.key' \
    "$work/external-secrets.yaml")
  log "ok: every ExternalSecret reads only its namespace's parameters"

  # The Pod Identity key of each component is also its app name.
  local sa
  for app in external-secrets aws-lb-controller; do
    sa="$(jq -r --arg k "$app" '.pod_identities[$k] | "\(.namespace)/\(.service_account)"' "$CONTRACT")"
    expect "Pod Identity service account for $app" \
      "$(SA="${sa#*/}" yq -N 'select(.kind == "Deployment" and .spec.template.spec.serviceAccountName == strenv(SA))
          | .metadata.namespace + "/" + .spec.template.spec.serviceAccountName' "$work/$app.yaml" | head -n 1)" \
      "$sa"
  done
  expect "LB controller tags what it creates" \
    "$(yq -N 'select(.kind == "Deployment") | .spec.template.spec.containers[0].args[] | select(test("^--default-tags="))' "$work/aws-lb-controller.yaml")" \
    "--default-tags=env=$(contract .env),project=$PROJECT"
  log "all checks passed"
}

main "$@"
