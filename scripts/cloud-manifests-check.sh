#!/usr/bin/env bash
# Offline checks for the AWS-only components sf-cloud owns (external-secrets, aws-lb-controller, opencost).
# platform-validate.sh already renders every app, runs kubeconform with the public CRD catalog and checks image
# digests; this adds what it cannot know:
#   1. External Secrets custom resources validated against the CRDs shipped in the pinned chart (the public
#      catalog has no schema for external-secrets.io/v1, so they would only be "skipped");
#   2. the manifests agree with infra/cloud-contract.json: Pod Identity service accounts, ESO namespaces, role
#      prefix and region, the LB controller's cost/reaper tags;
#   3. every ExternalSecret in the repo (each lives in its component's aws overlay) uses the ClusterSecretStore of
#      its own namespace, reads only /shopflow/aws/<namespace>/*, carries SkipDryRunOnMissingResource=true and
#      matches the CRD. An ExternalSecret in a namespace without a store fails.
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

# Render one Application's chart and path sources, as Argo CD would (session parameters: their placeholders).
render_app() {
  local app_file="$1" count i chart repo ns release dir vo
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
      vo="$(mktemp "$WORK_DIR/values-object.XXXXXX")"
      yq ".spec.sources[$i].helm.valuesObject // {}" "$app_file" >"$vo"
      values+=(--values "$vo")
      helm template "$release" "$chart" --repo "$repo" --version "$(yq ".spec.sources[$i].targetRevision" "$app_file")" \
        --namespace "$ns" --kube-version "$KUBERNETES_VERSION" --include-crds "${values[@]}"
    elif [ "$(yq ".spec.sources[$i].path // \"\"" "$app_file")" != "" ]; then
      dir="$REPO_ROOT/$(yq ".spec.sources[$i].path" "$app_file")"
      vo="$(mktemp "$WORK_DIR/values-object.XXXXXX")"
      yq ".spec.sources[$i].helm.valuesObject // {}" "$app_file" >"$vo"
      helm template "$release" "$dir" --namespace "$ns" --kube-version "$KUBERNETES_VERSION" --values "$vo"
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

# Every ExternalSecret in deploy/: plain manifests as they are, templated ones rendered with their chart's defaults.
collect_external_secrets() {
  local file chart
  while IFS= read -r file; do
    if grep -q '{{' "$file"; then
      chart="$(dirname "$(dirname "$file")")"
      [ -f "$chart/Chart.yaml" ] || fail "$file is templated but $chart has no Chart.yaml"
      helm template x "$chart" --kube-version "$KUBERNETES_VERSION" | yq 'select(.kind == "ExternalSecret")' ||
        fail "$chart does not render"
    else
      yq 'select(.kind == "ExternalSecret")' "$file"
    fi
    echo "---"
  done < <(grep -rlE '^kind:[[:space:]]*ExternalSecret' "$REPO_ROOT/deploy" --include='*.yaml' | sort)
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

  # 1. External Secrets CRs (stores here, ExternalSecrets from every overlay) against the pinned chart's CRDs.
  crd_schemas "$work/external-secrets.yaml" "$work/schemas"
  collect_external_secrets >"$work/external-secret-list.yaml"
  { yq 'select(.apiVersion == "external-secrets.io/*")' "$work/external-secrets.yaml"; echo "---"; cat "$work/external-secret-list.yaml"; } |
    yq 'select(. != null)' >"$work/eso-crs.yaml"
  kubeconform -strict -summary -kubernetes-version "$KUBERNETES_VERSION" \
    -schema-location "$work/schemas/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" "$work/eso-crs.yaml" ||
    fail "External Secrets resources do not match the chart's CRDs"

  # 2. Contract.
  local stores_values="$REPO_ROOT/deploy/platform/external-secrets/aws/secret-stores/values.yaml"
  expect "ESO namespaces = eso_namespaces" "$(yq -o=json -I=0 '.namespaces' "$stores_values")" "$(jq -c .eso_namespaces "$CONTRACT")"
  expect "ESO role prefix" "$(yq '.rolePrefix' "$stores_values")" "$(contract .roles.eso_prefix)"
  expect "ESO region" "$(yq '.region' "$stores_values")" "$REGION"
  expect "one ClusterSecretStore per namespace" \
    "$(yq -N 'select(.kind == "ClusterSecretStore") | .spec.conditions[0].namespaces[0]' "$work/external-secrets.yaml" | sort | tr '\n' ' ')" \
    "$(jq -r '.eso_namespaces[]' "$CONTRACT" | sort | tr '\n' ' ')"

  # 3. ExternalSecrets of every component.
  local count name ns store key options
  count="$(yq -N 'select(.kind == "ExternalSecret") | .metadata.name' "$work/external-secret-list.yaml" | grep -c . || true)"
  while IFS=$'\t' read -r ns name store options; do
    [ -n "$name" ] || continue
    jq -e --arg ns "$ns" '.eso_namespaces | index($ns)' "$CONTRACT" >/dev/null ||
      fail "ExternalSecret $ns/$name: namespace $ns has no ClusterSecretStore (add it to eso_namespaces)"
    [ "$store" = "ClusterSecretStore/ssm-$ns" ] || fail "ExternalSecret $ns/$name must use ClusterSecretStore/ssm-$ns, not $store"
    case "$options" in *SkipDryRunOnMissingResource=true*) ;; *) fail "ExternalSecret $ns/$name: add the SkipDryRunOnMissingResource=true sync option" ;; esac
  done < <(yq -N 'select(.kind == "ExternalSecret") | [.metadata.namespace, .metadata.name,
      (.spec.secretStoreRef.kind // "SecretStore") + "/" + .spec.secretStoreRef.name,
      .metadata.annotations["argocd.argoproj.io/sync-options"] // ""] | @tsv' "$work/external-secret-list.yaml")
  while IFS=$'\t' read -r ns name key; do
    [ -n "$name" ] || continue
    case "$key" in "$SSM_PREFIX/$ns/"*) ;; *) fail "ExternalSecret $ns/$name reads $key, outside $SSM_PREFIX/$ns/" ;; esac
  done < <(yq -N 'select(.kind == "ExternalSecret") | .metadata.namespace as $ns | .metadata.name as $name
      | [(.spec.data // [])[] | .remoteRef.key] + [(.spec.dataFrom // [])[] | (.extract.key // .find.path // "")]
      | .[] | [$ns, $name, .] | @tsv' "$work/external-secret-list.yaml")
  log "ok: $count ExternalSecret(s) use their namespace's store and parameters"

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
