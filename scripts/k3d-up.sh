#!/usr/bin/env bash
# Create (or reuse) a k3d cluster with a local registry, install Argo CD once, then hand everything else to
# GitOps: one root Application per profile, each tracking GIT_REVISION of github.com/winthebest/shopflow.
#
# Environment:
#   CLUSTER            cluster name (default sf-main); ports come from docs/contracts/environment.md
#   PROFILES           comma-separated directories under deploy/argocd/profiles (default core)
#   GIT_REVISION       branch, tag or commit SHA that Argo CD tracks (default main); must exist on origin
#   WAIT_TIMEOUT       seconds to wait for every Application to be Synced + Healthy (default 900; 0 = no wait)
#   SOPS_AGE_KEY_FILE  age private key (default ~/.config/sops/age/keys.txt); copied into the cluster, never printed
#   PULL_CACHE         1 (default) = pull images through the shared caches (ADR 0207); 0 = straight from upstream
set -euo pipefail

# shellcheck source=scripts/k3d-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/k3d-lib.sh"

PROFILES="${PROFILES:-core}"
GIT_REVISION="${GIT_REVISION:-main}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-900}"
PULL_CACHE="${PULL_CACHE:-1}"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"

# k3s v1.35.5 (k3d v5.9 default), pinned by digest.
K3S_IMAGE="rancher/k3s:v1.35.5-k3s1@sha256:2074403abe1bded11ef3dde09d457e13be8e0b64c218b1c4f8269b4565cfbc65"
ARGOCD_CHART_FILE="$ROOT_DIR/deploy/argocd/bootstrap/argocd-chart.yaml"
ARGOCD_VALUES="$ROOT_DIR/deploy/argocd/bootstrap/values.yaml"
ARGOCD_ADMIN_SECRET="$ROOT_DIR/deploy/secrets/local/argocd-admin.enc.yaml"
ROOT_APP_TEMPLATE="$ROOT_DIR/deploy/argocd/root-app.yaml"

preflight() {
  require docker k3d kubectl helm sops yq jq htpasswd git
  docker info >/dev/null 2>&1 || die "Docker is not running"
  [[ -r "$SOPS_AGE_KEY_FILE" ]] || die "age key not found at $SOPS_AGE_KEY_FILE (docs/runbooks/local-platform.md)"
  sops -d "$ARGOCD_ADMIN_SECRET" >/dev/null 2>&1 || die "cannot decrypt $ARGOCD_ADMIN_SECRET with $SOPS_AGE_KEY_FILE"

  local profile
  IFS=',' read -ra PROFILE_LIST <<<"$PROFILES"
  for profile in "${PROFILE_LIST[@]}"; do
    [[ "$profile" != _* && -f "$ROOT_DIR/deploy/argocd/profiles/$profile/kustomization.yaml" ]] \
      || die "unknown profile: $profile"
  done
  # obs and obs-lite both own the otel-gateway release (docs/contracts/gitops.md §4).
  if [[ ",$PROFILES," == *",obs,"* && ",$PROFILES," == *",obs-lite,"* ]]; then
    die "PROFILES cannot contain both obs and obs-lite; pick one"
  fi

  # Argo CD reads Git from GitHub, not from this checkout, so the revision must be pushed. Resolve it to the
  # commit SHA that every Application must report before `make up` calls the cluster ready.
  if [[ "$GIT_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    TARGET_SHA="$GIT_REVISION"
  else
    TARGET_SHA="$(git -C "$ROOT_DIR" ls-remote origin "$GIT_REVISION" | awk -v rev="$GIT_REVISION" '
      $2 == "refs/heads/" rev { head = $1 } $2 == "refs/tags/" rev "^{}" { peeled = $1 } $2 == "refs/tags/" rev { tag = $1 }
      END { print (head != "" ? head : (peeled != "" ? peeled : tag)) }')"
    [[ -n "$TARGET_SHA" ]] || die "revision '$GIT_REVISION' not found on origin; push it first"
  fi
  if [[ "$GIT_REVISION" == "$(git -C "$ROOT_DIR" branch --show-current)" ]] \
    && [[ -n "$(git -C "$ROOT_DIR" log --oneline "origin/$GIT_REVISION..HEAD" 2>/dev/null)" ]]; then
    log "warning: local commits on $GIT_REVISION are not pushed; Argo CD will not see them"
  fi
}

# Start the shared pull-through caches (one per upstream registry), creating them on first use. Their data lives
# in named volumes, so images downloaded once per machine survive `make down`.
ensure_caches() {
  local entry name upstream port
  for entry in "${REGISTRY_CACHES[@]}"; do
    read -r name upstream port <<<"$entry"
    if docker container inspect "k3d-$name" >/dev/null 2>&1; then
      docker start "k3d-$name" >/dev/null
    else
      log "creating pull-through cache k3d-$name -> $upstream (127.0.0.1:$port)"
      k3d registry create "$name" --image "$REGISTRY_CACHE_IMAGE" --port "127.0.0.1:$port" \
        --proxy-remote-url "$upstream" --volume "$name:/var/lib/registry" --no-help >/dev/null
    fi
  done
}

# k3d flags that route node image pulls through the caches. Only takes effect when a cluster is created.
cache_args() {
  [[ "$PULL_CACHE" == "1" ]] || return 0
  local entry name port
  for entry in "${REGISTRY_CACHES[@]}"; do
    read -r name _ port <<<"$entry"
    printf '%s\n' --registry-use "k3d-$name:$port"
  done
  printf '%s\n' --registry-config "$ROOT_DIR/scripts/k3d-registries.yaml"
}

create_cluster() {
  if cluster_exists; then
    log "cluster exists; making sure it is running"
    k3d cluster start "$CLUSTER" >/dev/null
  else
    log "creating cluster (API 127.0.0.1:$API_PORT, HTTPS 127.0.0.1:$HTTPS_PORT, registry 127.0.0.1:$REGISTRY_PORT, pull cache: $PULL_CACHE)"
    [[ "$PULL_CACHE" != "1" ]] || ensure_caches
    local extra_args=()
    while IFS= read -r arg; do extra_args+=("$arg"); done < <(cache_args)
    # 1 server + 1 agent to save RAM; Traefik off (Envoy Gateway is the edge); servicelb (klipper) stays on and
    # backs the Gateway's LoadBalancer Service, which the k3d load balancer exposes on HTTPS_PORT.
    k3d cluster create "$CLUSTER" \
      --image "$K3S_IMAGE" \
      --servers 1 --agents 1 \
      --api-port "127.0.0.1:$API_PORT" \
      --port "127.0.0.1:$HTTPS_PORT:443@loadbalancer" \
      --k3s-arg "--disable=traefik@server:0" \
      --registry-create "$REGISTRY_NAME:127.0.0.1:$REGISTRY_PORT" \
      --kubeconfig-update-default=false \
      ${extra_args[@]+"${extra_args[@]}"} \
      --wait --timeout 300s
  fi
  # Add the context to ~/.kube/config without switching the current context (other sessions use it).
  k3d kubeconfig merge "$CLUSTER" --kubeconfig-merge-default --kubeconfig-switch-context=false >/dev/null
  kc wait --for=condition=Ready nodes --all --timeout=180s >/dev/null
}

# Helm values fragment with the bcrypt hash of the admin password from SOPS. Passed to Helm through process
# substitution, so neither the password nor its hash touches the disk or the process list.
admin_password_values() {
  local hash
  # shellcheck disable=SC2016 # $2y$ and $2a$ are literal bcrypt prefixes
  hash="$(sops -d --extract '["stringData"]["password"]' "$ARGOCD_ADMIN_SECRET" \
    | htpasswd -niBC 10 admin | cut -d: -f2- | tr -d '\n' | sed 's/^\$2y\$/$2a$/')"
  printf 'configs:\n  secret:\n    argocdServerAdminPassword: "%s"\n    argocdServerAdminPasswordMtime: "%s"\n' \
    "$hash" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

install_argocd() {
  log "installing Argo CD (chart $(yq '.chart + " " + .version' "$ARGOCD_CHART_FILE"))"
  kc create namespace argocd --dry-run=client -o yaml | kc apply -f - >/dev/null
  # age key for KSOPS: file -> API server, nothing echoed.
  kc -n argocd create secret generic sops-age --from-file=keys.txt="$SOPS_AGE_KEY_FILE" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
  local password_values
  password_values="$(admin_password_values)"
  # shellcheck disable=SC2016 # literal bcrypt prefix
  [[ "$password_values" == *'"$2a$'* ]] || die "could not derive the Argo CD admin password hash"
  helm upgrade --install argocd "$(yq '.chart' "$ARGOCD_CHART_FILE")" \
    --repo "$(yq '.repo' "$ARGOCD_CHART_FILE")" \
    --version "$(yq '.version' "$ARGOCD_CHART_FILE")" \
    --kube-context "$KUBE_CONTEXT" --namespace argocd \
    --values "$ARGOCD_VALUES" \
    --values <(printf '%s\n' "$password_values") \
    --wait --timeout 10m >/dev/null
}

apply_root_apps() {
  local profile
  for profile in "${PROFILE_LIST[@]}"; do
    log "root app: root-$profile -> deploy/argocd/profiles/$profile @ $GIT_REVISION"
    PROFILE="$profile" REVISION="$GIT_REVISION" yq '
      .metadata.name = "root-" + strenv(PROFILE) |
      .metadata.labels."shopflow.io/profile" = strenv(PROFILE) |
      .spec.source.path = "deploy/argocd/profiles/" + strenv(PROFILE) |
      .spec.source.targetRevision = strenv(REVISION) |
      .spec.source.kustomize.patches[0].patch =
        "- op: replace\n  path: /data/revision\n  value: \"" + strenv(REVISION) + "\""
    ' "$ROOT_APP_TEMPLATE" | kc apply -f - >/dev/null
    # Reconcile now instead of at the next poll, so the new revision is picked up right away.
    kc -n argocd annotate applications.argoproj.io "root-$profile" argocd.argoproj.io/refresh=normal --overwrite >/dev/null
  done
  # Root apps of profiles that are no longer requested keep running; say so instead of deleting them silently.
  local extra
  extra="$(kc -n argocd get applications.argoproj.io -o name | sed -n 's|.*/root-||p' \
    | grep -vxF -f <(printf '%s\n' "${PROFILE_LIST[@]}") || true)"
  [[ -z "$extra" ]] || log "warning: root apps for other profiles still exist: $(echo "$extra" | tr '\n' ' ')"
}

# name, sync, health, and whether the app has synced TARGET_SHA or a later commit (stale status from the
# previous revision must not count as ready; a branch that moves on while we wait must not cause a timeout).
app_table() {
  local name sync health revisions rev state
  while IFS=$'\t' read -r name sync health revisions; do
    state="old-revision"
    for rev in ${revisions//,/ }; do
      if [[ "$rev" == "$TARGET_SHA" ]] || is_descendant "$rev"; then state="current"; break; fi
    done
    printf '%s\t%s\t%s\t%s\n' "$name" "$sync" "$health" "$state"
  done < <(kc -n argocd get applications.argoproj.io -o json | jq -r '
    .items[] | [.metadata.name, (.status.sync.status // "Unknown"), (.status.health.status // "Unknown"),
      ([.status.sync.revision // empty] + (.status.sync.revisions // []) | join(","))] | @tsv')
}

# True if commit $1 contains TARGET_SHA (fetches when either commit is not known locally).
is_descendant() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]] || return 1
  { git -C "$ROOT_DIR" cat-file -e "$1^{commit}" && git -C "$ROOT_DIR" cat-file -e "$TARGET_SHA^{commit}"; } 2>/dev/null \
    || git -C "$ROOT_DIR" fetch -q origin 2>/dev/null || true
  git -C "$ROOT_DIR" merge-base --is-ancestor "$TARGET_SHA" "$1" 2>/dev/null
}

wait_for_apps() {
  [[ "$WAIT_TIMEOUT" -gt 0 ]] || return 0
  log "waiting up to ${WAIT_TIMEOUT}s for all Applications to be Synced + Healthy at ${TARGET_SHA:0:12}"
  local deadline=$((SECONDS + WAIT_TIMEOUT)) table pending
  while :; do
    table="$(app_table)"
    pending="$(awk -F'\t' '$2 != "Synced" || $3 != "Healthy" || $4 != "current"' <<<"$table")"
    if [[ -n "$table" && -z "$pending" ]]; then
      column -t <<<"$table" >&2
      return 0
    fi
    if ((SECONDS >= deadline)); then
      column -t <<<"$table" >&2
      die "timed out; inspect with: make status CLUSTER=$CLUSTER"
    fi
    log "pending: $(awk -F'\t' '{printf "%s(%s/%s/%s) ", $1, $2, $3, $4}' <<<"$pending")"
    sleep 15
  done
}

preflight
create_cluster
install_argocd
apply_root_apps
wait_for_apps
log "ready in ${SECONDS}s. Argo CD UI: make platform-argocd-ui CLUSTER=$CLUSTER (password: make platform-argocd-password)"
