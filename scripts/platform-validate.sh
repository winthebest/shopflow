#!/usr/bin/env bash
# Render everything Argo CD deploys from deploy/ and validate it (used by platform-ci and `make platform-validate`):
#   1. every profile passes the root app's revision to every source from this repo (fail-closed check);
#   2. every Application in deploy/argocd/apps renders (Helm chart sources, Kustomize or Helm path sources),
#      plus the Argo CD bootstrap chart;
#   3. the rendered manifests pass kubeconform (Kubernetes schemas + CRDs-catalog);
#   4. no rendered workload uses an image without a digest.
# Works on a temporary copy of deploy/ with KSOPS generators removed: CI has no age key, and the in-cluster KSOPS
# path is exercised by `make up`. Needs: kubectl (kustomize), helm, yq, kubeconform.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL="https://github.com/winthebest/shopflow.git"
KUBERNETES_VERSION="${KUBERNETES_VERSION:-1.34.12}"   # newest schema set published for kubeconform
OUT_DIR="${OUT_DIR:-$ROOT_DIR/out/platform-validate}"
CHECK_REVISION="platform-validate-revision"

fail() { printf 'platform-validate: %s\n' "$*" >&2; exit 1; }
for tool in kubectl helm yq kubeconform; do
  command -v "$tool" >/dev/null 2>&1 || fail "missing tool: $tool"
done

# Argo CD must build with Kustomize's default load restrictor, like this script does (ADR 0204).
build_options="$(yq '.configs.cm."kustomize.buildOptions" // ""' "$ROOT_DIR/deploy/argocd/bootstrap/values.yaml")"
[[ "$build_options" != *load-restrictor* ]] \
  || fail "deploy/argocd/bootstrap/values.yaml: kustomize.buildOptions must not change the load restrictor"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
rm -rf "$OUT_DIR" && mkdir -p "$OUT_DIR"
cp -R "$ROOT_DIR/deploy" "$WORK/deploy"

# Drop KSOPS generators from the copy (their Secrets cannot be decrypted here). First check statically what
# KSOPS would hit in the cluster: every listed file exists relative to the kustomization directory (KSOPS
# resolves `files` from where kustomize runs, not from the generator file) and is SOPS-encrypted.
while IFS= read -r kfile; do
  dir="$(dirname "$kfile")"
  rel="${dir#"$WORK/"}"
  for gen in $(yq '.generators[]?' "$kfile"); do
    if [[ "$(yq '.kind' "$dir/$gen")" == "ksops" ]]; then
      for enc in $(yq '.files[]' "$dir/$gen"); do
        [[ -f "$dir/$enc" ]] || fail "$rel/$gen: $enc not found relative to $rel (KSOPS paths start at the kustomization directory)"
        [[ "$(yq '.sops.mac // ""' "$dir/$enc")" != "" ]] || fail "$rel/$enc is not SOPS-encrypted"
      done
      GEN="$gen" yq -i 'del(.generators[] | select(. == strenv(GEN)))' "$kfile"
    fi
  done
done < <(find "$WORK/deploy" -name kustomization.yaml)

build() { kubectl kustomize "$1"; }

# 1. Profiles: simulate the root app's patch and require every shopflow source to follow it.
for profile_dir in "$WORK"/deploy/argocd/profiles/*/; do
  profile="$(basename "$profile_dir")"
  [[ "$profile" == _* ]] && continue
  REVISION="$CHECK_REVISION" yq -i '.patches += [{
    "target": {"kind": "ConfigMap", "name": "git-revision"},
    "patch": "- op: replace\n  path: /data/revision\n  value: " + strenv(REVISION)
  }]' "$profile_dir/kustomization.yaml"
  build "$profile_dir" > "$OUT_DIR/profile-$profile.yaml" || fail "profile $profile does not build"
  stale="$(REPO="$REPO_URL" REVISION="$CHECK_REVISION" yq -N '
    select(.kind == "Application")
    | select([.spec.sources[] | select((.repoURL == strenv(REPO)) and (.targetRevision != strenv(REVISION)))] | length > 0)
    | .metadata.name
  ' "$OUT_DIR/profile-$profile.yaml")"
  [[ -z "$stale" ]] || fail "profile $profile: these apps ignore the root revision: $stale"
  echo "profile $profile: $(yq -N 'select(.kind == "Application") | .metadata.name' "$OUT_DIR/profile-$profile.yaml" | wc -l | tr -d ' ') apps follow the root revision"
done

# 2. Render each source of an Application the way Argo CD would.
render_chart() { # app_file index name namespace
  local app="$1" s=".spec.sources[$2]" name="$3" ns="$4" repo chart args=() vf
  repo="$(yq "$s.repoURL" "$app")"
  chart="$(yq "$s.chart" "$app")"
  args=(template "$(yq "$s.helm.releaseName // \"$name\"" "$app")")
  if [[ "$repo" =~ ^https?:// ]]; then args+=("$chart" --repo "$repo"); else args+=("oci://$repo/$chart"); fi
  args+=(--version "$(yq "$s.targetRevision" "$app")" --namespace "$ns" --kube-version "$KUBERNETES_VERSION")
  [[ "$(yq "$s.helm.skipCrds // false" "$app")" == "true" ]] || args+=(--include-crds)
  while IFS= read -r vf; do
    [[ -n "$vf" ]] && args+=(--values "${vf/\$values/$WORK}")
  done < <(yq "$s.helm.valueFiles[]?" "$app")
  if [[ "$(yq "$s.helm.valuesObject // \"\"" "$app")" != "" ]]; then
    yq "$s.helm.valuesObject" "$app" > "$WORK/values-$name-$2.yaml"
    args+=(--values "$WORK/values-$name-$2.yaml")
  fi
  helm "${args[@]}"
}

render_path() { # app_file index name namespace
  local app="$1" s=".spec.sources[$2]" name="$3" ns="$4" dir args=() vf
  dir="$WORK/$(yq "$s.path" "$app")"
  if [[ -f "$dir/kustomization.yaml" ]]; then
    build "$dir"
  elif [[ -f "$dir/Chart.yaml" ]]; then
    args=(template "$(yq "$s.helm.releaseName // \"$name\"" "$app")" "$dir" --namespace "$ns"
      --kube-version "$KUBERNETES_VERSION" --include-crds)
    while IFS= read -r vf; do
      [[ -z "$vf" ]] && continue
      if [[ "$vf" == \$values/* ]]; then args+=(--values "${vf/\$values/$WORK}"); else args+=(--values "$dir/$vf"); fi
    done < <(yq "$s.helm.valueFiles[]?" "$app")
    helm "${args[@]}"
  else
    find "$dir" -maxdepth 1 -name '*.yaml' -exec sh -c 'cat "$1"; echo "---"' _ {} \;
  fi
}

for app_dir in "$WORK"/deploy/argocd/apps/*/; do
  build "$app_dir" > "$WORK/apps.yaml" || fail "$(basename "$app_dir"): app does not build"
  count="$(yq -N 'select(.kind == "Application") | .metadata.name' "$WORK/apps.yaml" | wc -l | tr -d ' ')"
  for ((doc = 0; doc < count; doc++)); do
    app="$WORK/app-$doc.yaml"
    DOC="$doc" yq ea -N '[select(.kind == "Application")] | .[env(DOC)]' "$WORK/apps.yaml" > "$app"
    name="$(yq '.metadata.name' "$app")"
    ns="$(yq '.spec.destination.namespace // "default"' "$app")"
    [[ "$(yq '.spec | has("source")' "$app")" == "false" ]] || fail "$name: use spec.sources (a list), not spec.source"
    out="$OUT_DIR/app-$name.yaml"
    cp "$app" "$out"
    sources="$(yq '.spec.sources | length' "$app")"
    for ((i = 0; i < sources; i++)); do
      echo "---" >> "$out"
      if [[ "$(yq ".spec.sources[$i].chart // \"\"" "$app")" != "" ]]; then
        render_chart "$app" "$i" "$name" "$ns" >> "$out" || fail "$name: chart source $i does not render"
      elif [[ "$(yq ".spec.sources[$i].path // \"\"" "$app")" != "" ]]; then
        render_path "$app" "$i" "$name" "$ns" >> "$out" || fail "$name: path source $i does not render"
      fi
    done
    echo "app $name: rendered $sources source(s)"
  done
done

# Argo CD bootstrap chart (installed by k3d-up.sh) and the root app template.
helm template argocd "$(yq '.chart' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --repo "$(yq '.repo' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --version "$(yq '.version' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --namespace argocd --kube-version "$KUBERNETES_VERSION" --include-crds \
  --values "$ROOT_DIR/deploy/argocd/bootstrap/values.yaml" > "$OUT_DIR/bootstrap-argocd.yaml"
cp "$ROOT_DIR/deploy/argocd/root-app.yaml" "$OUT_DIR/root-app.yaml"
echo "bootstrap: Argo CD chart rendered"

# 3. Schemas. CRs whose CRD is not in the catalog are reported as skipped in the summary.
kubeconform -strict -summary -kubernetes-version "$KUBERNETES_VERSION" \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  -ignore-missing-schemas \
  "$OUT_DIR"/*.yaml

# 4. Every image reference in rendered workloads/CRs must carry a digest.
undigested="$(yq -N 'select(.kind != "CustomResourceDefinition" and .kind != "Application")' "$OUT_DIR"/*.yaml \
  | grep -hoE '(image|imageName):[[:space:]]*"?[^"[:space:]]+|--[a-z0-9-]*image=[^"[:space:]]+' \
  | sed -E 's/^(image|imageName):[[:space:]]*"?//; s/^--[a-z0-9-]*image=//' \
  | grep -v '@sha256:[0-9a-f]\{64\}$' | sort -u || true)"
[[ -z "$undigested" ]] || fail "images without a digest:"$'\n'"$undigested"
echo "images: all pinned by digest"
