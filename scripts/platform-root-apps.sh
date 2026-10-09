#!/usr/bin/env bash
# Apply one root Application per profile (docs/adr/0206-root-apps-overlays-and-params.md). One mechanism for
# `make up` (--overlay local, scripts/k3d-up.sh) and `make cloud-up` (--overlay aws, scripts/cloud-up.sh).
#
# Usage: platform-root-apps.sh --overlay local|aws --revision REV --profiles p1,p2 [--param key=value ...] [--check]
#   --overlay   local: deploy/argocd/profiles/<p>; aws: deploy/argocd/profiles-aws/<p>
#   --revision  branch, tag or SHA every child app tracks (passed through the git-revision ConfigMap)
#   --param     session parameter, declared in deploy/argocd/profiles/_common/platform-params.yaml; aws profiles
#               copy it into Helm values. Unknown keys are refused; keys the file marks required-aws must be set.
#   --check     validate the arguments only (no cluster access)
#   --print     print the root Applications instead of applying them (no cluster access)
# Kubernetes context: KUBE_CONTEXT if set, otherwise the current context of KUBECONFIG.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARAMS_FILE="$ROOT_DIR/deploy/argocd/profiles/_common/platform-params.yaml"
ROOT_APP_TEMPLATE="$ROOT_DIR/deploy/argocd/root-app.yaml"

log() { printf '\033[1;34m[root-apps]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[root-apps] error:\033[0m %s\n' "$*" >&2; exit 1; }
kc() { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} "$@"; }

OVERLAY="" REVISION="" PROFILES="" CHECK_ONLY=0 PRINT_ONLY=0
PARAMS=()
while (($#)); do
  case "$1" in
    --overlay) OVERLAY="${2:?--overlay needs a value}"; shift ;;
    --revision) REVISION="${2:?--revision needs a value}"; shift ;;
    --profiles) PROFILES="${2:?--profiles needs a value}"; shift ;;
    --param) PARAMS+=("${2:?--param needs key=value}"); shift ;;
    --check) CHECK_ONLY=1 ;;
    --print) PRINT_ONLY=1 ;;
    *) die "unknown argument: $1 (usage: --overlay local|aws --revision REV --profiles p1,p2 [--param k=v]...)" ;;
  esac
  shift
done

case "$OVERLAY" in
  local) PROFILES_DIR="deploy/argocd/profiles" ;;
  aws) PROFILES_DIR="deploy/argocd/profiles-aws" ;;
  *) die "--overlay must be local or aws" ;;
esac
[[ -n "$REVISION" ]] || die "--revision is required"
[[ -n "$PROFILES" ]] || die "--profiles is required"
IFS=',' read -ra PROFILE_LIST <<<"$PROFILES"

has_profile() { [[ ",$PROFILES," == *",$1,"* ]]; }

validate_profiles() {
  local profile
  for profile in "${PROFILE_LIST[@]}"; do
    [[ "$profile" != _* && -f "$ROOT_DIR/$PROFILES_DIR/$profile/kustomization.yaml" ]] \
      || die "unknown profile for overlay $OVERLAY: $profile ($PROFILES_DIR/$profile)"
  done
  # obs and obs-lite both own the otel-gateway release; data's ServiceMonitors and rules need the CRDs that
  # either of them installs (docs/contracts/gitops.md §4).
  if has_profile obs && has_profile obs-lite; then
    die "PROFILES cannot contain both obs and obs-lite; pick one"
  fi
  if has_profile data && ! has_profile obs && ! has_profile obs-lite; then
    die "profile data needs obs or obs-lite in the same PROFILES (e.g. PROFILES=core,obs-lite,data)"
  fi
}

# Parameters: only declared keys, values on one line; on aws every required key must be set (fail-closed: a
# missing value would otherwise deploy a chart default, e.g. an empty VPC ID or a placeholder account).
validate_params() {
  local entry key value required
  for entry in ${PARAMS[@]+"${PARAMS[@]}"}; do
    [[ "$entry" == *=* ]] || die "--param needs key=value, got: $entry"
    key="${entry%%=*}" value="${entry#*=}"
    KEY="$key" yq -e '.data | has(strenv(KEY))' "$PARAMS_FILE" >/dev/null 2>&1 \
      || die "unknown parameter $key (declare it in deploy/argocd/profiles/_common/platform-params.yaml)"
    [[ "$value" != *$'\n'* ]] || die "parameter $key must be a single line"
  done
  [[ "$OVERLAY" == aws ]] || return 0
  for required in $(yq '.metadata.annotations."shopflow.io/required-aws" // "" | split(",") | .[]' "$PARAMS_FILE"); do
    value=""
    for entry in ${PARAMS[@]+"${PARAMS[@]}"}; do
      [[ "${entry%%=*}" == "$required" ]] && value="${entry#*=}"
    done
    [[ -n "$value" ]] || die "overlay aws needs --param $required=<value>"
  done
}

# Application names a profile deploys (from the local checkout of its kustomization).
profile_apps() {
  kubectl kustomize "$ROOT_DIR/$PROFILES_DIR/$1" | yq -N 'select(.kind == "Application") | .metadata.name'
}

# obs and obs-lite are exclusive. When PROFILES asks for one and the cluster still runs the other, delete the old
# root app first (non-cascading, so it cannot re-create anything), then the child apps only it had (their own
# finalizers remove their resources). Apps both profiles share stay running and are adopted by the new root app.
remove_excluded_profiles() {
  local pair wanted excluded keep app p
  for pair in "obs obs-lite" "obs-lite obs"; do
    read -r wanted excluded <<<"$pair"
    has_profile "$wanted" || continue
    kc -n argocd get applications.argoproj.io "root-$excluded" >/dev/null 2>&1 || continue
    log "PROFILES has $wanted: removing root-$excluded and the apps only it deploys"
    keep="$(for p in "${PROFILE_LIST[@]}"; do profile_apps "$p"; done)"
    # Drop any resources finalizer first: deleting the root app must never cascade into the shared apps.
    kc -n argocd patch applications.argoproj.io "root-$excluded" --type merge -p '{"metadata":{"finalizers":null}}' >/dev/null
    kc -n argocd delete applications.argoproj.io "root-$excluded" --cascade=orphan --wait=true --timeout=120s >/dev/null
    for app in $(profile_apps "$excluded"); do
      grep -qxF "$app" <<<"$keep" && continue
      log "  deleting app $app (only in $excluded)"
      kc -n argocd delete applications.argoproj.io "$app" --ignore-not-found --wait=true --timeout=300s >/dev/null
    done
  done
}

# JSON patch for the platform-params ConfigMap: one `add` per --param (keys may contain dots, never `/` or `~`).
params_patch() {
  local entry
  for entry in ${PARAMS[@]+"${PARAMS[@]}"}; do
    jq -nc --arg k "${entry%%=*}" --arg v "${entry#*=}" '{op: "add", path: ("/data/" + $k), value: $v}'
  done | jq -sc '.'
}

# Root Application for profile $1 with params patch $2, from deploy/argocd/root-app.yaml.
render_root_app() {
    PROFILE="$1" PATCH="$2" OVERLAY="$OVERLAY" DIR="$PROFILES_DIR" REV="$REVISION" yq '
      .metadata.name = "root-" + strenv(PROFILE) |
      .metadata.labels."shopflow.io/profile" = strenv(PROFILE) |
      .metadata.labels."shopflow.io/overlay" = strenv(OVERLAY) |
      .spec.source.path = strenv(DIR) + "/" + strenv(PROFILE) |
      .spec.source.targetRevision = strenv(REV) |
      .spec.source.kustomize.patches[0].patch =
        "- op: replace\n  path: /data/revision\n  value: \"" + strenv(REV) + "\"" |
      .spec.source.kustomize.patches += (
        [{"target": {"kind": "ConfigMap", "name": "platform-params"}, "patch": strenv(PATCH)}]
        | map(select(.patch != "[]")))
    ' "$ROOT_APP_TEMPLATE"
}

apply_root_apps() {
  local profile patch
  patch="$(params_patch)"
  if ((PRINT_ONLY)); then
    for profile in "${PROFILE_LIST[@]}"; do echo "---"; render_root_app "$profile" "$patch"; done
    return 0
  fi
  remove_excluded_profiles
  for profile in "${PROFILE_LIST[@]}"; do
    log "root-$profile -> $PROFILES_DIR/$profile @ $REVISION"
    render_root_app "$profile" "$patch" | kc apply -f - >/dev/null
    # Reconcile now instead of at the next poll, so the new revision and params are picked up right away.
    kc -n argocd annotate applications.argoproj.io "root-$profile" argocd.argoproj.io/refresh=normal --overwrite >/dev/null
  done
  # Root apps of profiles that are no longer requested keep running; say so instead of deleting them silently.
  local extra
  extra="$(kc -n argocd get applications.argoproj.io -o name | sed -n 's|.*/root-||p' \
    | grep -vxF -f <(printf '%s\n' "${PROFILE_LIST[@]}") || true)"
  [[ -z "$extra" ]] || log "warning: root apps for other profiles still exist: $(echo "$extra" | tr '\n' ' ')"
}

validate_profiles
validate_params
((CHECK_ONLY)) && exit 0
apply_root_apps
