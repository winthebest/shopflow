#!/usr/bin/env bash
# Render everything Argo CD deploys from deploy/ and validate it (used by platform-ci and `make platform-validate`):
#   1. every profile passes the root app's revision to every source from this repo (fail-closed check);
#   2. every Application in deploy/argocd/apps renders (Helm chart sources, Kustomize or Helm path sources),
#      plus the Argo CD bootstrap chart;
#   3. the rendered manifests pass kubeconform (Kubernetes schemas + CRDs-catalog);
#   4. no rendered workload uses an image without a digest;
#   5. the image of the shop migration Job (tag sha-<commit>) contains the newest commit under
#      services/<service>/migrations, so a chart bump cannot ship an image without a migration (needs full Git history).
#      SHOP_MIGRATION_CHECK=fail (default) exits on a stale image; warn prints a GitHub annotation and goes on.
#      Every shop service must run the same tag (always fails otherwise).
# Works on a temporary copy of deploy/ with KSOPS generators removed: CI has no age key, and the in-cluster KSOPS
# path is exercised by `make up`. Needs: git, kubectl (kustomize), helm, yq, kubeconform.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL="https://github.com/winthebest/shopflow.git"
KUBERNETES_VERSION="${KUBERNETES_VERSION:-1.34.12}"   # newest schema set published for kubeconform
OUT_DIR="${OUT_DIR:-$ROOT_DIR/out/platform-validate}"
CHECK_REVISION="platform-validate-revision"
SHOP_MIGRATION_CHECK="${SHOP_MIGRATION_CHECK:-fail}"

fail() { printf 'platform-validate: %s\n' "$*" >&2; exit 1; }
[[ "$SHOP_MIGRATION_CHECK" == fail || "$SHOP_MIGRATION_CHECK" == warn ]] \
  || fail "SHOP_MIGRATION_CHECK must be fail or warn, got: $SHOP_MIGRATION_CHECK"
for tool in git kubectl helm yq kubeconform; do
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

# 1. Profiles: simulate the root app's patches and require every shopflow source to follow the revision. aws
#    profiles also get every declared session param (dummy values), so their replacements must resolve.
PARAMS_FILE="$WORK/deploy/argocd/profiles/_common/platform-params.yaml"
params_patch="$(yq -o=json '.data | keys | map({"op": "add", "path": "/data/" + ., "value": "validate-" + .})' "$PARAMS_FILE" | jq -c .)"
for profile_dir in "$WORK"/deploy/argocd/profiles/*/ "$WORK"/deploy/argocd/profiles-aws/*/; do
  [[ -d "$profile_dir" ]] || continue
  profile="$(basename "$profile_dir")"
  [[ "$profile" == _* ]] && continue
  overlay=local
  [[ "$profile_dir" == */profiles-aws/* ]] && overlay=aws
  REVISION="$CHECK_REVISION" yq -i '.patches += [{
    "target": {"kind": "ConfigMap", "name": "git-revision"},
    "patch": "- op: replace\n  path: /data/revision\n  value: " + strenv(REVISION)
  }]' "$profile_dir/kustomization.yaml"
  if [[ "$overlay" == aws ]]; then
    PATCH="$params_patch" yq -i '.patches += [{"target": {"kind": "ConfigMap", "name": "platform-params"}, "patch": strenv(PATCH)}]' \
      "$profile_dir/kustomization.yaml"
  fi
  # Every param a profile reads must be declared (a typo would otherwise copy nothing and keep a chart default).
  for key in $(yq '.replacements[]? | select(.source.name == "platform-params") | .source.fieldPath' "$profile_dir/kustomization.yaml" \
    | sed -n -e 's/^data\.\[\(.*\)\]$/\1/p' -e 's/^data\.\([^.[]*\)$/\1/p'); do
    KEY="$key" yq -e '.data | has(strenv(KEY))' "$PARAMS_FILE" >/dev/null 2>&1 \
      || fail "profile $overlay/$profile reads undeclared param $key"
  done
  out="$OUT_DIR/profile-$overlay-$profile.yaml"
  build "$profile_dir" > "$out" || fail "profile $overlay/$profile does not build"
  stale="$(REPO="$REPO_URL" REVISION="$CHECK_REVISION" yq -N '
    select(.kind == "Application")
    | select([.spec.sources[] | select((.repoURL == strenv(REPO)) and (.targetRevision != strenv(REVISION)))] | length > 0)
    | .metadata.name
  ' "$out")"
  [[ -z "$stale" ]] || fail "profile $overlay/$profile: these apps ignore the root revision: $stale"
  echo "profile $overlay/$profile: $(yq -N 'select(.kind == "Application") | .metadata.name' "$out" | wc -l | tr -d ' ') apps follow the root revision"
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
    # valuesObject (session params on aws, ADR 0206) wins over the value files, as in Argo CD.
    if [[ "$(yq "$s.helm.valuesObject // \"\"" "$app")" != "" ]]; then
      yq "$s.helm.valuesObject" "$app" > "$WORK/values-$name-$2.yaml"
      args+=(--values "$WORK/values-$name-$2.yaml")
    fi
    helm "${args[@]}"
  else
    find "$dir" -maxdepth 1 -name '*.yaml' -exec sh -c 'cat "$1"; echo "---"' _ {} \;
  fi
}

# render_apps <file with Applications> <output prefix>: one output file per Application, its sources rendered.
render_apps() {
  local apps="$1" prefix="$2" count doc app name ns out sources i
  count="$(yq -N 'select(.kind == "Application") | .metadata.name' "$apps" | wc -l | tr -d ' ')"
  for ((doc = 0; doc < count; doc++)); do
    app="$WORK/app-$doc.yaml"
    DOC="$doc" yq ea -N '[select(.kind == "Application")] | .[env(DOC)]' "$apps" > "$app"
    name="$(yq '.metadata.name' "$app")"
    ns="$(yq '.spec.destination.namespace // "default"' "$app")"
    [[ "$(yq '.spec | has("source")' "$app")" == "false" ]] || fail "$name: use spec.sources (a list), not spec.source"
    out="$OUT_DIR/$prefix$name.yaml"
    [[ -f "$out" ]] && continue
    cp "$app" "$out"
    sources="$(yq '.spec.sources | length' "$app")"
    for ((i = 0; i < sources; i++)); do
      echo "---" >> "$out"
      if [[ "$(yq ".spec.sources[$i].chart // \"\"" "$app")" != "" ]]; then
        render_chart "$app" "$i" "$name" "$ns" >> "$out" || fail "$prefix$name: chart source $i does not render"
      elif [[ "$(yq ".spec.sources[$i].path // \"\"" "$app")" != "" ]]; then
        render_path "$app" "$i" "$name" "$ns" >> "$out" || fail "$prefix$name: path source $i does not render"
      fi
    done
    echo "app $prefix$name: rendered $sources source(s)"
  done
}

# Local apps straight from their directories (also those no profile lists yet).
for app_dir in "$WORK"/deploy/argocd/apps/*/; do
  build "$app_dir" > "$WORK/apps.yaml" || fail "$(basename "$app_dir"): app does not build"
  render_apps "$WORK/apps.yaml" "app-"
done
# aws apps as their aws profiles produce them (variants from apps-aws/, session params filled in).
for out in "$OUT_DIR"/profile-aws-*.yaml; do
  [[ -f "$out" ]] && render_apps "$out" "app-aws-"
done

# Argo CD bootstrap chart (installed by k3d-up.sh) and the root app template.
helm template argocd "$(yq '.chart' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --repo "$(yq '.repo' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --version "$(yq '.version' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --namespace argocd --kube-version "$KUBERNETES_VERSION" --include-crds \
  --values "$ROOT_DIR/deploy/argocd/bootstrap/values.yaml" > "$OUT_DIR/bootstrap-argocd.yaml"
helm template argocd "$(yq '.chart' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --repo "$(yq '.repo' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --version "$(yq '.version' "$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml")" \
  --namespace argocd --kube-version "$KUBERNETES_VERSION" --include-crds \
  --values "$ROOT_DIR/deploy/argocd/bootstrap/values.yaml" \
  --values "$ROOT_DIR/deploy/argocd/bootstrap/values-aws.yaml" > "$OUT_DIR/bootstrap-argocd-aws.yaml"
! grep -qE 'ksops|sops-age|enable-exec' "$OUT_DIR/bootstrap-argocd-aws.yaml" \
  || fail "bootstrap values-aws.yaml: KSOPS, the age key mount or exec plugins are still enabled on AWS"
cp "$ROOT_DIR/deploy/argocd/root-app.yaml" "$OUT_DIR/root-app.yaml"
echo "bootstrap: Argo CD chart rendered (local and aws)"

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

# 5. The migration Job runs the image of services.<migration.service>; the commit in its tag must contain the newest
#    migration, otherwise the release deploys a schema older than what the rest of deploy/ relies on (e.g. the CDC
#    publication that Debezium reads). platform-ci fails on main and on PRs that change the shop chart, and only warns
#    on other PRs, so a pending bump after a migration does not block every lane.
shop_values="$ROOT_DIR/deploy/charts/shop/values.yaml"
migration_service="$(yq '.migration.service' "$shop_values")"
migration_tag="$(SVC="$migration_service" yq '.services[strenv(SVC)].image.tag' "$shop_values")"
[[ "$migration_tag" =~ ^sha-([0-9a-f]{7,40})$ ]] \
  || fail "deploy/charts/shop: services.$migration_service.image.tag must be sha-<commit>, got: $migration_tag"
tag_ref="${BASH_REMATCH[1]}"
# Images are published together (one tag for gateway, orders, payments): a bump that misses a service is an error.
mismatched="$(TAG="$migration_tag" yq '.services | to_entries | map(select(.value.image.tag != strenv(TAG)))
  | map(.key + "=" + (.value.image.tag // "none")) | join(", ")' "$shop_values")"
[[ -z "$mismatched" ]] \
  || fail "deploy/charts/shop: every service must run tag $migration_tag (as services.$migration_service), got: $mismatched"
[[ "$(git -C "$ROOT_DIR" rev-parse --is-shallow-repository)" == false ]] \
  || fail "shallow Git clone: the migration check needs full history (actions/checkout fetch-depth: 0)"
tag_commit="$(git -C "$ROOT_DIR" rev-parse --verify -q "$tag_ref^{commit}")" \
  || fail "deploy/charts/shop: image tag $migration_tag is not a commit of this repository"
latest_migration="$(git -C "$ROOT_DIR" log -1 --format=%H -- "services/$migration_service/migrations")"
[[ -n "$latest_migration" ]] || fail "no commit touches services/$migration_service/migrations"
if git -C "$ROOT_DIR" merge-base --is-ancestor "$latest_migration" "$tag_commit"; then
  echo "migrations: $migration_service image $migration_tag contains ${latest_migration:0:7}"
else
  stale="deploy/charts/shop: image $migration_tag (services.$migration_service) predates migration commit"
  stale+=" ${latest_migration:0:7}; bump the shop images to a tag built from it or later"
  [[ "$SHOP_MIGRATION_CHECK" == warn ]] || fail "$stale"
  echo "::warning title=Shop migration image::$stale"
fi
