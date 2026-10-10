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
APPS=(kube-prometheus-stack loki tempo otel-collector otel-collector-lite slo grafana-dashboards chaos-mesh)
AWS_APPS=(kube-prometheus-stack loki tempo otel-collector otel-collector-lite chaos-mesh)   # deploy/argocd/apps-aws/<app>
COMPONENTS=(kube-prometheus-stack loki tempo otel-collector slo grafana-dashboards chaos-mesh)   # deploy/platform/<c>
PROFILES=(obs-lite obs chaos)   # deploy/argocd/profiles/<p> and deploy/argocd/profiles-aws/<p>
# Game-day experiments (chaos/*.yaml) may only target these namespaces (annotated chaos-mesh.org/inject=enabled).
CHAOS_NAMESPACES=(shop kafka)

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

# SLOs: every slo/<name>.yaml (Sloth spec) generates RULES_DIR/<name>-slo.prometheusrule.yaml. Hand-written
# rules (e.g. checkout-sli-guard) live next to them; unit tests are slo/tests/*.test.yaml. RULES_DIR's
# kustomization lists every *.prometheusrule.yaml and is rewritten by `slo` (Kustomize has no globs).
SLO_DIR="slo"
SLO_WINDOWS="slo/windows"
SLO_PERIOD="28d"
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

# Render one Argo Application file (every chart source + every repo path source) into one multi-doc YAML.
render_app() {
  local app="$1" file="$2" ns n i src
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
        --namespace "$ns" --kube-version "$KUBE_VERSION" --api-versions cert-manager.io/v1 "${args[@]}"
    elif [[ "$(yq "$src.path // \"\"" "$file")" != "" ]]; then
      [[ "$(yq "$src.repoURL" "$file")" == "$SHOPFLOW_REPO" ]] || fail "$app: path source outside this repo"
      kubectl kustomize "$OUT/work/$(yq "$src.path" "$file")"
    fi
    echo "---"
  done
}

# rendered/<app>.yaml for the local apps, rendered/aws-<app>.yaml for the aws variants (apps-aws/ patches applied).
cmd_render() {
  rm -rf "$OUT/rendered" && mkdir -p "$OUT/rendered" "$OUT/argocd"
  prepare_work_copy
  local app
  for app in "${APPS[@]}"; do
    log "render $app"
    render_app "$app" "$ROOT/deploy/argocd/apps/$app/application.yaml" > "$OUT/rendered/$app.yaml"
  done
  for app in "${AWS_APPS[@]}"; do
    log "render aws/$app"
    kubectl kustomize "$ROOT/deploy/argocd/apps-aws/$app" > "$OUT/argocd/aws-app-$app.yaml"
    render_app "$app" "$OUT/argocd/aws-app-$app.yaml" > "$OUT/rendered/aws-$app.yaml"
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
  for app in "${AWS_APPS[@]}"; do
    docs+=("$OUT/argocd/aws-app-$app.yaml")
  done
  # Profiles need deploy/argocd/profiles/_common (sf-platform). sf-platform's platform-ci checks the
  # revision wiring of every profile; here we only check that ours build.
  if [[ -d "$ROOT/deploy/argocd/profiles/_common" ]]; then
    for prof in "${PROFILES[@]}"; do
      kubectl kustomize "$ROOT/deploy/argocd/profiles/$prof" > "$OUT/argocd/profile-$prof.yaml"
      kubectl kustomize "$ROOT/deploy/argocd/profiles-aws/$prof" > "$OUT/argocd/profile-aws-$prof.yaml"
      docs+=("$OUT/argocd/profile-$prof.yaml" "$OUT/argocd/profile-aws-$prof.yaml")
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

# Sloth specs, as repo-relative paths (slo/*.yaml, not windows/ or tests/).
slo_specs() {
  local f
  for f in "$ROOT/$SLO_DIR"/*.yaml; do
    [[ -e "$f" ]] && echo "$SLO_DIR/$(basename "$f")"
  done
}

# sloth generate <repo-relative spec> <output path seen from /repo or /out> <docker mount args...>
sloth_generate() {
  local spec="$1" out="$2"; shift 2
  docker_run "$@" -w /repo "$SLOTH_IMAGE" generate -i "$spec" -o "$out" \
    --slo-period-windows-path="$SLO_WINDOWS" --default-slo-period="$SLO_PERIOD" >/dev/null
}

# The kustomization of RULES_DIR as it must be: every *.prometheusrule.yaml, sorted.
expected_rules_kustomization() {
  local f
  echo "# Generated by \`make sre-slo\` from the *.prometheusrule.yaml files in this directory; CI fails on drift."
  echo "apiVersion: kustomize.config.k8s.io/v1beta1"
  echo "kind: Kustomization"
  echo "resources:"
  for f in "$ROOT/$RULES_DIR"/*.prometheusrule.yaml; do
    [[ -e "$f" ]] && echo "  - $(basename "$f")"
  done
}

# Regenerate every SLO PrometheusRule from its Sloth spec + the rules kustomization (writes into the repo).
cmd_slo() {
  local spec name
  while IFS= read -r spec; do
    name="$(basename "$spec" .yaml)"
    log "sloth generate $spec → $RULES_DIR/$name-slo.prometheusrule.yaml"
    sloth_generate "$spec" "$RULES_DIR/$name-slo.prometheusrule.yaml" -v "$ROOT:/repo"
  done < <(slo_specs)
  expected_rules_kustomization > "$ROOT/$RULES_DIR/kustomization.yaml"
  log "$RULES_DIR/kustomization.yaml lists every rule file"
}

# Fail if any committed SLO rule differs from its spec, has no spec, or is missing from the kustomization.
cmd_slo_drift() {
  local spec name f
  rm -rf "$OUT/slo" && mkdir -p "$OUT/slo"
  while IFS= read -r spec; do
    name="$(basename "$spec" .yaml)"
    docker_run -v "$ROOT:/repo:ro" -w /repo "$SLOTH_IMAGE" validate -i "$spec" \
      --slo-period-windows-path="$SLO_WINDOWS" --default-slo-period="$SLO_PERIOD" >/dev/null
    sloth_generate "$spec" "/out/$name-slo.prometheusrule.yaml" -v "$ROOT:/repo:ro" -v "$OUT/slo:/out"
    [[ -f "$ROOT/$RULES_DIR/$name-slo.prometheusrule.yaml" ]] \
      || fail "$RULES_DIR/$name-slo.prometheusrule.yaml missing: run 'make sre-slo' and commit the result"
    diff -u "$ROOT/$RULES_DIR/$name-slo.prometheusrule.yaml" "$OUT/slo/$name-slo.prometheusrule.yaml" \
      || fail "$RULES_DIR/$name-slo.prometheusrule.yaml is stale: run 'make sre-slo' and commit the result"
    log "SLO rules match $spec"
  done < <(slo_specs)
  for f in "$ROOT/$RULES_DIR"/*-slo.prometheusrule.yaml; do
    [[ -e "$f" ]] || continue
    name="$(basename "$f" -slo.prometheusrule.yaml)"
    [[ -f "$ROOT/$SLO_DIR/$name.yaml" ]] || fail "$f has no spec $SLO_DIR/$name.yaml (generated rules without a source)"
  done
  diff -u "$ROOT/$RULES_DIR/kustomization.yaml" <(expected_rules_kustomization) \
    || fail "$RULES_DIR/kustomization.yaml does not list every rule file: run 'make sre-slo'"
  log "$RULES_DIR/kustomization.yaml lists every rule file"
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

# Validate the rendered configs with the binaries that will run them, for each variant ("" = local, "aws-").
cmd_configs() {
  [[ -d "$OUT/rendered" ]] || cmd_render
  rm -rf "$OUT/configs"
  validate_configs ""
  validate_configs aws-
}

validate_configs() {
  local v="$1" dir="$OUT/configs/${1:-local-}"
  dir="${dir%-}"
  mkdir -p "$dir/otel" "$dir/loki" "$dir/tempo" "$dir/alertmanager"
  local app cm found
  for app in "${v}otel-collector" "${v}otel-collector-lite"; do
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
    "$OUT/rendered/${v}loki.yaml" > "$dir/loki/config.yaml"
  log "loki -verify-config (${v:-local-}loki)"
  docker_run -v "$dir/loki:/cfg:ro" "$LOKI_IMAGE" -config.file=/cfg/config.yaml -verify-config
  yq 'select(.kind == "ConfigMap" and .metadata.name == "tempo") | .data["tempo.yaml"]' \
    "$OUT/rendered/${v}tempo.yaml" | sed 's#/conf/overrides.yaml#/cfg/overrides.yaml#' > "$dir/tempo/tempo.yaml"
  yq 'select(.kind == "ConfigMap" and .metadata.name == "tempo") | .data["overrides.yaml"]' \
    "$OUT/rendered/${v}tempo.yaml" > "$dir/tempo/overrides.yaml"
  log "tempo -config.verify (${v:-local-}tempo)"
  docker_run -v "$dir/tempo:/cfg:ro" "$TEMPO_IMAGE" -config.file=/cfg/tempo.yaml -config.verify=true
  local am='select(.kind == "Secret" and .metadata.name == "alertmanager-kps-alertmanager")'
  yq "$am | .data[\"alertmanager.yaml\"]" "$OUT/rendered/${v}kube-prometheus-stack.yaml" | base64 -d \
    | sed 's#/etc/alertmanager/config/#/cfg/#' > "$dir/alertmanager/alertmanager.yaml"
  yq "$am | .data[\"shopflow.tmpl\"]" "$OUT/rendered/${v}kube-prometheus-stack.yaml" | base64 -d \
    > "$dir/alertmanager/shopflow.tmpl"
  grep -q 'webhook_url_file' "$dir/alertmanager/alertmanager.yaml" || fail "Alertmanager webhook must be read from a file"
  log "amtool check-config (${v:-local-}kube-prometheus-stack)"
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
  # KSOPS resolves `files` from the kustomization root, not from the generator file's directory.
  local kfile kdir gen enc
  for c in "${COMPONENTS[@]}"; do
    while IFS= read -r kfile; do
      kdir="$(dirname "$kfile")"
      for gen in $(yq '.generators[]?' "$kfile"); do
        [[ "$(yq '.kind' "$kdir/$gen")" == "ksops" ]] || continue
        for enc in $(yq '.files[]' "$kdir/$gen"); do
          [[ -f "$kdir/$enc" ]] || fail "$kdir/$gen: '$enc' not found relative to the kustomization root $kdir"
        done
      done
    done < <(find "$ROOT/deploy/platform/$c" -name kustomization.yaml)
  done
  log "secrets: every KSOPS file path resolves from its kustomization root"
  [[ -d "$OUT/rendered" ]] || cmd_render
  # Image fields and image flags (e.g. --prometheus-config-reloader=, --thanos-default-base-image=), same rule as
  # scripts/platform-validate.sh.
  local unpinned
  unpinned="$({ yq -N '.. | select(tag == "!!map" and has("image")) | .image | select(tag == "!!str")' "$OUT"/rendered/*.yaml
    grep -hoE -- '--[a-z0-9-]*(image|reloader)=[^"[:space:]]+' "$OUT"/rendered/*.yaml | sed -E 's/^--[a-z0-9-]*=//'; } \
    | sort -u | grep -v '@sha256:' || true)"
  [[ -z "$unpinned" ]] || fail "images not pinned by digest:"$'\n'"$unpinned"
  log "images: all pinned by digest"
  # Every Secret a rendered workload/CR needs must come from somewhere in the same environment. Local: the render
  # itself (Secrets, cert-manager Certificates) or a SOPS file in deploy/ (KSOPS). AWS: the render itself, including
  # ExternalSecret targets (no SOPS there). A Secret normally made by a disabled chart Job (e.g. the operator's kps-admission TLS cert), or an aws
  # overlay without its ExternalSecret, is caught here instead of as a pod stuck in ContainerCreating.
  local local_files=() aws_files=() f
  for f in "$OUT"/rendered/*.yaml; do
    if [[ "$(basename "$f")" == aws-* ]]; then aws_files+=("$f"); else local_files+=("$f"); fi
  done
  check_secret_refs local "${local_files[@]}"
  check_secret_refs aws "${aws_files[@]}"
}

# check_secret_refs <local|aws> <rendered files...>: fail on any referenced Secret that nothing creates.
check_secret_refs() {
  local env="$1" refs known missing; shift
  refs="$({ yq -N '.. | select(tag == "!!map") | select(has("secretName") and (.optional // false) != true) | .secretName' "$@"
    yq -N '.. | select(tag == "!!map") | select(has("secretKeyRef") and (.secretKeyRef.optional // false) != true) | .secretKeyRef.name' "$@"
    yq -N '.. | select(tag == "!!map") | select(has("secretRef") and (.secretRef.optional // false) != true) | .secretRef.name' "$@"
    yq -N 'select(.kind == "Alertmanager" or .kind == "Prometheus") | .spec.secrets[]?' "$@"; } | sort -u)"
  known="$({ yq -N 'select(.kind == "Secret") | .metadata.name' "$@"
    yq -N 'select(.kind == "ExternalSecret") | .spec.target.name // .metadata.name' "$@"
    yq -N 'select(.kind == "Certificate") | .spec.secretName' "$@"
    if [[ "$env" == local ]]; then
      find "$ROOT/deploy" -path '*/secrets/*.enc.yaml' -exec yq '.metadata.name' {} \;
    fi; } | sort -u)"
  missing="$(comm -23 <(echo "$refs") <(echo "$known") | grep -v '^$' || true)"
  [[ -z "$missing" ]] || fail "$env: Secrets referenced but never created:"$'\n'"$missing"
  local source=SOPS; [[ "$env" == aws ]] && source=ExternalSecret
  log "secrets ($env): every referenced Secret is created by the render or $source"
}

# Game days: experiments are schema-checked against the CRDs of the pinned Chaos Mesh chart (the public catalog has
# none), may only target the allowed namespaces, must stop by themselves unless one-shot (pod-kill), and no profile
# other than `chaos` may install Chaos Mesh (it must be gone outside game days).
cmd_chaos() {
  local app="$ROOT/deploy/argocd/apps/chaos-mesh/application.yaml" dir="$OUT/chaos" crd f bad prof
  rm -rf "$dir" && mkdir -p "$dir/charts" "$dir/schemas/chaos-mesh.org"
  helm pull "$(yq '.spec.sources[0].chart' "$app")" --repo "$(yq '.spec.sources[0].repoURL' "$app")" \
    --version "$(yq '.spec.sources[0].targetRevision' "$app")" --untar -d "$dir/charts" >/dev/null
  for crd in "$dir"/charts/chaos-mesh/crds/*.yaml; do
    yq -o=json '.' "$crd" | jq -c '.spec as $s | $s.versions[] | {kind: ($s.names.kind | ascii_downcase), version: .name,
      schema: (.schema.openAPIV3Schema | walk(if type == "object" and has("properties") and (has("additionalProperties") | not)
        and ((.["x-kubernetes-preserve-unknown-fields"] // false) | not) then . + {additionalProperties: false} else . end))}' \
    | while IFS= read -r line; do
        jq '.schema' <<<"$line" > "$dir/schemas/chaos-mesh.org/$(jq -r '.kind + "_" + .version' <<<"$line").json"
      done
  done
  log "kubeconform chaos/*.yaml (CRD schemas of the pinned chart, strict)"
  kubeconform -strict -summary -kubernetes-version "$KUBE_VERSION" \
    -schema-location "$dir/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" "$ROOT"/chaos/*.yaml
  local allowed; allowed="$(printf '%s\n' "${CHAOS_NAMESPACES[@]}" | jq -R . | jq -sc .)"
  for f in "$ROOT"/chaos/*.yaml; do
    bad="$(yq -o=json '.' "$f" | jq -r --argjson ok "$allowed" '
      [ (if (.metadata.namespace // "") as $n | $ok | index($n) | not then "metadata.namespace not allowed" else empty end),
        (if ((.spec.selector.namespaces // []) | length) == 0 then "spec.selector.namespaces is required" else empty end),
        ((.spec.selector.namespaces // [])[] | select(. as $n | $ok | index($n) | not) | "selector namespace \(.) not allowed"),
        (if (.spec.action != "pod-kill") and ((.spec.duration // "") == "") then "spec.duration required (auto-stop)" else empty end)
      ] | join("; ")')"
    [[ -z "$bad" ]] || fail "$(basename "$f"): $bad"
  done
  log "chaos experiments: allowed namespaces only (${CHAOS_NAMESPACES[*]}), auto-stop unless one-shot"
  for prof in "$ROOT"/deploy/argocd/profiles/*/kustomization.yaml "$ROOT"/deploy/argocd/profiles-aws/*/kustomization.yaml; do
    [[ "$(basename "$(dirname "$prof")")" == chaos ]] && continue
    ! yq '.resources[]?' "$prof" | grep -q 'chaos-mesh' || fail "$prof lists chaos-mesh: only profile chaos may"
  done
  log "Chaos Mesh is listed only by profile chaos"
}

usage() {
  cat >&2 <<EOF
usage: $0 <step>...
  render       render every sre Argo app (Helm + Kustomize) into $OUT/rendered
  kubeconform  schema-check rendered manifests, app directories and profiles
  slo          regenerate $RULES_DIR/<name>-slo.prometheusrule.yaml from every $SLO_DIR/<name>.yaml (writes to the repo)
  slo-drift    fail if a generated rule file or the rules kustomization is stale
  rules        runbook links + promtool check/test of our PrometheusRules
  configs      validate OTel Collector, Loki, Tempo and Alertmanager configs with their own binaries
  lint         shellcheck + dashboards JSON + encrypted secrets + image digest pinning
  chaos        game-day experiments: schema, allowed namespaces, auto-stop; chaos-mesh only in profile chaos
  all          render kubeconform slo-drift rules configs lint chaos
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
    chaos) cmd_chaos ;;
    all) cmd_render; cmd_kubeconform; cmd_slo_drift; cmd_rules; cmd_configs; cmd_lint; cmd_chaos ;;
    *) usage ;;
  esac
done
