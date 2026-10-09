#!/usr/bin/env bash
# Offline checks for the sf-sre components (observability stack + SLOs). No cluster needed.
# Used by mk/sre.mk and .github/workflows/sre-ci.yml; run `make help` for the targets.
#
# Single source of truth: chart repo/version/release/valueFiles are read from deploy/argocd/apps/<app>/
# application.yaml, so what CI renders is exactly what Argo CD will install.
#
# Needs: helm, kubectl (kustomize), yq v4, jq, docker; kubeconform for the `kubeconform` step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${SRE_OUT:-$ROOT/out/sre}"
KUBE_VERSION="${KUBE_VERSION:-1.34.12}"   # same schema set as scripts/platform-validate.sh
SHOPFLOW_REPO="https://github.com/winthebest/shopflow.git"
APPS=(kube-prometheus-stack loki tempo otel-collector otel-collector-lite slo grafana-dashboards)
COMPONENTS=(kube-prometheus-stack loki tempo otel-collector slo grafana-dashboards)   # deploy/platform/<c>
PROFILES=(obs-lite obs)

# Tool images, pinned by digest (multi-arch index).
SLOTH_IMAGE="ghcr.io/slok/sloth:v0.16.0@sha256:f0f0075b0d45c1cf684e92947508cc1d5bf573925f785f803a439b2306c8b9a5"
PROMETHEUS_IMAGE="quay.io/prometheus/prometheus:v3.15.0@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e"
ALERTMANAGER_IMAGE="quay.io/prometheus/alertmanager:v0.34.1@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a"
OTELCOL_IMAGE="otel/opentelemetry-collector-k8s:0.161.0@sha256:2fed8ff024bdb42473890724fa0833bd31e16197aa4790d8e0be3829f87f0de1"
LOKI_IMAGE="grafana/loki:3.7.8@sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193"
TEMPO_IMAGE="grafana/tempo:3.1.0@sha256:3076b8dcdfb32fd6bc5ccef85e7b7313e6199b9cb84366257fc17ecb696db5fd"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d"
# CRD JSON schemas for kubeconform, pinned to a commit of datreeio/CRDs-catalog.
CRD_CATALOG="https://raw.githubusercontent.com/datreeio/CRDs-catalog/63669a570e231d4f1f8396d229a1de512bcf0a34"

SLO_SPEC="slo/checkout.yaml"
SLO_WINDOWS="slo/windows"
SLO_PERIOD="28d"
SLO_RULES="deploy/platform/slo/base/checkout-slo.prometheusrule.yaml"
RULES_DIR="deploy/platform/slo/base"
RUNBOOK_PREFIX="https://github.com/winthebest/shopflow/blob/main/"

# Keep helm's repo index/cache inside out/ so CI and laptops render hermetically.
export HELM_CACHE_HOME="$OUT/helm/cache" HELM_CONFIG_HOME="$OUT/helm/config" HELM_DATA_HOME="$OUT/helm/data"

log() { printf '==> %s\n' "$*" >&2; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
docker_run() { docker run --rm -u "$(id -u):$(id -g)" "$@"; }

# Copy deploy/ to $OUT/work without KSOPS generators: CI has no age key (decryption is exercised in-cluster
# by `make up`). Same approach as scripts/platform-validate.sh.
prepare_work_copy() {
  local work="$OUT/work" kfile dir gen
  rm -rf "$work" && mkdir -p "$work" && cp -R "$ROOT/deploy" "$work/deploy"
  while IFS= read -r kfile; do
    dir="$(dirname "$kfile")"
    for gen in $(yq '.generators[]?' "$kfile"); do
      if [[ "$(yq '.kind' "$dir/$gen")" == "ksops" ]]; then
        GEN="$gen" yq -i 'del(.generators[] | select(. == strenv(GEN)))' "$kfile"
      fi
    done
  done < <(find "$work/deploy" -name kustomization.yaml)
}

# Render one Argo Application (every chart source + every repo path source) into one multi-doc YAML.
render_app() {
  local app="$1" file="$ROOT/deploy/argocd/apps/$1/application.yaml" ns n i src
  ns="$(yq '.spec.destination.namespace' "$file")"
  n="$(yq '.spec.sources | length' "$file")"
  for ((i = 0; i < n; i++)); do
    src=".spec.sources[$i]"
    if [[ "$(yq "$src.chart // \"\"" "$file")" != "" ]]; then
      local args=() vf
      while IFS= read -r vf; do
        [[ -n "$vf" ]] && args+=(-f "$ROOT/${vf#\$values/}")
      done < <(yq "$src.helm.valueFiles[]" "$file")
      helm template "$(yq "$src.helm.releaseName // \"$app\"" "$file")" "$(yq "$src.chart" "$file")" \
        --repo "$(yq "$src.repoURL" "$file")" --version "$(yq "$src.targetRevision" "$file")" \
        --namespace "$ns" --kube-version "$KUBE_VERSION" "${args[@]}"
    elif [[ "$(yq "$src.path // \"\"" "$file")" != "" ]]; then
      [[ "$(yq "$src.repoURL" "$file")" == "$SHOPFLOW_REPO" ]] || fail "$app: path source outside this repo"
      kubectl kustomize "$OUT/work/$(yq "$src.path" "$file")"
    fi
    echo "---"
  done
}

cmd_render() {
  mkdir -p "$OUT/rendered"
  prepare_work_copy
  local app
  for app in "${APPS[@]}"; do
    log "render $app"
    render_app "$app" > "$OUT/rendered/$app.yaml"
  done
}

cmd_kubeconform() {
  [[ -d "$OUT/rendered" ]] || cmd_render
  local docs=("$OUT"/rendered/*.yaml) app prof
  mkdir -p "$OUT/argocd"
  for app in "${APPS[@]}"; do
    kubectl kustomize "$ROOT/deploy/argocd/apps/$app" > "$OUT/argocd/app-$app.yaml"
    docs+=("$OUT/argocd/app-$app.yaml")
  done
  # Profiles need deploy/argocd/profiles/_common (sf-platform). sf-platform's platform-ci checks the
  # revision wiring of every profile; here we only check that ours build.
  if [[ -d "$ROOT/deploy/argocd/profiles/_common" ]]; then
    for prof in "${PROFILES[@]}"; do
      kubectl kustomize "$ROOT/deploy/argocd/profiles/$prof" > "$OUT/argocd/profile-$prof.yaml"
      docs+=("$OUT/argocd/profile-$prof.yaml")
    done
  else
    log "skip profile build: deploy/argocd/profiles/_common is not on this branch yet"
  fi
  log "kubeconform (Kubernetes $KUBE_VERSION + CRD catalog)"
  kubeconform -strict -summary -kubernetes-version "$KUBE_VERSION" \
    -schema-location default \
    -schema-location "$CRD_CATALOG/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
    "${docs[@]}"
}

# Regenerate the SLO PrometheusRule from the Sloth spec (writes into the repo).
cmd_slo() {
  log "sloth generate $SLO_SPEC → $SLO_RULES"
  docker_run -v "$ROOT:/repo" -w /repo "$SLOTH_IMAGE" generate -i "$SLO_SPEC" -o "$SLO_RULES" \
    --slo-period-windows-path="$SLO_WINDOWS" --default-slo-period="$SLO_PERIOD" >/dev/null
}

# Fail if the committed rules differ from what the spec generates.
cmd_slo_drift() {
  mkdir -p "$OUT/slo"
  docker_run -v "$ROOT:/repo:ro" -v "$OUT/slo:/out" -w /repo "$SLOTH_IMAGE" validate -i "$SLO_SPEC" \
    --slo-period-windows-path="$SLO_WINDOWS" --default-slo-period="$SLO_PERIOD" >/dev/null
  docker_run -v "$ROOT:/repo:ro" -v "$OUT/slo:/out" -w /repo "$SLOTH_IMAGE" generate -i "$SLO_SPEC" \
    -o /out/checkout-slo.prometheusrule.yaml --slo-period-windows-path="$SLO_WINDOWS" \
    --default-slo-period="$SLO_PERIOD" >/dev/null
  diff -u "$ROOT/$SLO_RULES" "$OUT/slo/checkout-slo.prometheusrule.yaml" \
    || fail "$SLO_RULES is stale: run 'make sre-slo' and commit the result"
  log "SLO rules match $SLO_SPEC"
}

# promtool check + unit tests on the .spec of every PrometheusRule we own; every alert needs a runbook
# that exists in this repo.
cmd_rules() {
  local dir="$OUT/rules" f name alert url
  rm -rf "$dir" && mkdir -p "$dir"
  for f in "$ROOT/$RULES_DIR"/*.prometheusrule.yaml; do
    name="$(basename "$f" .prometheusrule.yaml)"
    yq '.spec' "$f" > "$dir/$name.rules.yaml"
    while IFS=$'\t' read -r alert url; do
      [[ -n "$alert" ]] || continue
      [[ "$url" == "$RUNBOOK_PREFIX"* ]] || fail "$alert: runbook_url must start with $RUNBOOK_PREFIX (got '$url')"
      [[ -f "$ROOT/${url#"$RUNBOOK_PREFIX"}" ]] || fail "$alert: runbook ${url#"$RUNBOOK_PREFIX"} does not exist"
    done < <(yq -r '.spec.groups[].rules[] | select(has("alert")) | [.alert, (.annotations.runbook_url // "")] | @tsv' "$f")
  done
  log "runbook_url of every alert points to an existing file"
  cp "$ROOT"/slo/tests/*.test.yaml "$dir/"
  local rules=() tests=()
  for f in "$dir"/*.rules.yaml; do rules+=("$(basename "$f")"); done
  for f in "$dir"/*.test.yaml; do tests+=("$(basename "$f")"); done
  log "promtool check rules"
  docker_run -v "$dir:/rules:ro" -w /rules --entrypoint promtool "$PROMETHEUS_IMAGE" check rules "${rules[@]}"
  log "promtool test rules"
  docker_run -v "$dir:/rules:ro" -w /rules --entrypoint promtool "$PROMETHEUS_IMAGE" test rules "${tests[@]}"
}

# Validate the rendered configs with the binaries that will run them.
cmd_configs() {
  [[ -d "$OUT/rendered" ]] || cmd_render
  local dir="$OUT/configs"
  rm -rf "$dir" && mkdir -p "$dir/otel" "$dir/loki" "$dir/tempo" "$dir/alertmanager"
  local app cm found
  for app in otel-collector otel-collector-lite; do
    found=0
    # The chart stores the collector config under data.relay (gateway: <name>, DaemonSet: <name>-agent).
    for cm in $(yq -N 'select(.kind == "ConfigMap" and .data.relay != null) | .metadata.name' "$OUT/rendered/$app.yaml"); do
      yq "select(.kind == \"ConfigMap\" and .metadata.name == \"$cm\") | .data.relay" \
        "$OUT/rendered/$app.yaml" > "$dir/otel/$app-$cm.yaml"
      log "otelcol validate $app/$cm"
      docker_run -e MY_POD_IP=127.0.0.1 -e K8S_NODE_NAME=node -v "$dir/otel:/cfg:ro" "$OTELCOL_IMAGE" \
        validate --config="/cfg/$app-$cm.yaml"
      found=$((found + 1))
    done
    [[ $found -gt 0 ]] || fail "$app: no collector config rendered"
  done
  yq 'select(.kind == "ConfigMap" and .metadata.name == "loki") | .data["config.yaml"]' \
    "$OUT/rendered/loki.yaml" > "$dir/loki/config.yaml"
  log "loki -verify-config"
  docker_run -v "$dir/loki:/cfg:ro" "$LOKI_IMAGE" -config.file=/cfg/config.yaml -verify-config
  yq 'select(.kind == "ConfigMap" and .metadata.name == "tempo") | .data["tempo.yaml"]' \
    "$OUT/rendered/tempo.yaml" | sed 's#/conf/overrides.yaml#/cfg/overrides.yaml#' > "$dir/tempo/tempo.yaml"
  yq 'select(.kind == "ConfigMap" and .metadata.name == "tempo") | .data["overrides.yaml"]' \
    "$OUT/rendered/tempo.yaml" > "$dir/tempo/overrides.yaml"
  log "tempo -config.verify"
  docker_run -v "$dir/tempo:/cfg:ro" "$TEMPO_IMAGE" -config.file=/cfg/tempo.yaml -config.verify=true
  local am='select(.kind == "Secret" and .metadata.name == "alertmanager-kps-alertmanager")'
  yq "$am | .data[\"alertmanager.yaml\"]" "$OUT/rendered/kube-prometheus-stack.yaml" | base64 -d \
    | sed 's#/etc/alertmanager/config/#/cfg/#' > "$dir/alertmanager/alertmanager.yaml"
  yq "$am | .data[\"shopflow.tmpl\"]" "$OUT/rendered/kube-prometheus-stack.yaml" | base64 -d \
    > "$dir/alertmanager/shopflow.tmpl"
  grep -q 'webhook_url_file' "$dir/alertmanager/alertmanager.yaml" || fail "Alertmanager webhook must be read from a file"
  log "amtool check-config"
  docker_run -v "$dir/alertmanager:/cfg:ro" --entrypoint amtool "$ALERTMANAGER_IMAGE" check-config /cfg/alertmanager.yaml
}

# Lint: our scripts pass shellcheck; every dashboard is valid JSON with a unique uid; every rendered image is pinned.
cmd_lint() {
  local f uids
  log "shellcheck scripts/sre-*.sh"
  docker_run -v "$ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" -x scripts/sre-*.sh
  for f in "$ROOT"/deploy/platform/grafana-dashboards/base/dashboards/*.json; do
    jq -e '.uid and .title' "$f" >/dev/null || fail "$f: invalid dashboard JSON (needs uid and title)"
  done
  uids="$(jq -r '.uid' "$ROOT"/deploy/platform/grafana-dashboards/base/dashboards/*.json | sort | uniq -d)"
  [[ -z "$uids" ]] || fail "duplicate dashboard uid: $uids"
  log "dashboards: valid JSON, unique uids"
  local c bad
  for c in "${COMPONENTS[@]}"; do
    while IFS= read -r f; do
      yq -e '.sops.mac' "$f" >/dev/null 2>&1 || fail "$f: not a SOPS-encrypted file"
      bad="$(yq '[(.data // {}), (.stringData // {})] | .[] | to_entries[] | select(.value | test("^ENC\\[") | not) | .key' "$f")"
      [[ -z "$bad" ]] || fail "$f: plaintext values for: $bad"
    done < <(find "$ROOT/deploy/platform/$c" -path '*/secrets/*.enc.yaml')
  done
  log "secrets: every *.enc.yaml value is SOPS-encrypted"
  [[ -d "$OUT/rendered" ]] || cmd_render
  # Image fields and image flags (e.g. --prometheus-config-reloader=, --thanos-default-base-image=), same rule as
  # scripts/platform-validate.sh.
  local unpinned
  unpinned="$({ yq -N '.. | select(tag == "!!map" and has("image")) | .image | select(tag == "!!str")' "$OUT"/rendered/*.yaml
    grep -hoE -- '--[a-z0-9-]*(image|reloader)=[^"[:space:]]+' "$OUT"/rendered/*.yaml | sed -E 's/^--[a-z0-9-]*=//'; } \
    | sort -u | grep -v '@sha256:' || true)"
  [[ -z "$unpinned" ]] || fail "images not pinned by digest:"$'\n'"$unpinned"
  log "images: all pinned by digest"
}

usage() {
  cat >&2 <<EOF
usage: $0 <step>...
  render       render every sre Argo app (Helm + Kustomize) into $OUT/rendered
  kubeconform  schema-check rendered manifests, app directories and profiles
  slo          regenerate $SLO_RULES from $SLO_SPEC (writes to the repo)
  slo-drift    fail if $SLO_RULES is stale
  rules        runbook links + promtool check/test of our PrometheusRules
  configs      validate OTel Collector, Loki, Tempo and Alertmanager configs with their own binaries
  lint         shellcheck + dashboards JSON + encrypted secrets + image digest pinning
  all          render kubeconform slo-drift rules configs lint
EOF
  exit 2
}

[[ $# -gt 0 ]] || usage
for step in "$@"; do
  case "$step" in
    render) cmd_render ;;
    kubeconform) cmd_kubeconform ;;
    slo) cmd_slo ;;
    slo-drift) cmd_slo_drift ;;
    rules) cmd_rules ;;
    configs) cmd_configs ;;
    lint) cmd_lint ;;
    all) cmd_render; cmd_kubeconform; cmd_slo_drift; cmd_rules; cmd_configs; cmd_lint ;;
    *) usage ;;
  esac
done
