# Shared settings and helpers for scripts/k3d-*.sh. Source it; do not execute it.
# Cluster names and ports are a contract: docs/contracts/environment.md.

# shellcheck shell=bash
# shellcheck disable=SC2034 # variables are read by the scripts that source this file
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${CLUSTER:-sf-main}"
KUBE_CONTEXT="k3d-${CLUSTER}"

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$CLUSTER" "$*" >&2; }
die() { printf '\033[1;31m[%s] error:\033[0m %s\n' "$CLUSTER" "$*" >&2; exit 1; }

# kubectl bound to this cluster's context. Never relies on (or changes) the current context, because several
# sessions share ~/.kube/config and may be talking to other clusters at the same time.
kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }

require() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "missing tool: $tool"
  done
}

cluster_exists() { k3d cluster list "$CLUSTER" >/dev/null 2>&1; }

# Deterministic ports per cluster. On "address in use", stop the stale owner; never pick another port.
case "$CLUSTER" in
  sf-main)     _api_port=6550 _https_port=9443 ;;
  sf-platform) _api_port=6551 _https_port=8443 ;;
  sf-sre)      _api_port=6552 _https_port=8444 ;;
  sf-data)     _api_port=6553 _https_port=8445 ;;
  sf-app)      _api_port=6554 _https_port=8446 ;;
  *)           _api_port="" _https_port="" ;;
esac
API_PORT="${API_PORT:-$_api_port}"
HTTPS_PORT="${HTTPS_PORT:-$_https_port}"
[[ -n "$API_PORT" && -n "$HTTPS_PORT" ]] || die "cluster '$CLUSTER' is not in docs/contracts/environment.md; set API_PORT and HTTPS_PORT"
# Local registry port follows the API port (6550 -> 5050, 6551 -> 5051, ...).
REGISTRY_PORT="${REGISTRY_PORT:-$((API_PORT - 1500))}"
# Registry container (no "k3d-" prefix for --registry-create). Push to localhost:REGISTRY_PORT/<image>,
# reference it in the cluster as <CLUSTER>-registry:5000/<image>.
REGISTRY_NAME="${CLUSTER}-registry"
# Argo CD UI port-forward follows the API port too (6550 -> 18080, 6551 -> 18081, ...).
ARGOCD_UI_PORT="${ARGOCD_UI_PORT:-$((API_PORT + 11530))}"

# Pull-through image caches shared by every local cluster (docs/adr/0207-pull-through-image-cache.md):
# "<name> <upstream URL> <host port>". Anonymous upstreams only: no credentials are ever configured.
REGISTRY_CACHE_IMAGE="docker.io/library/registry:3.0.0@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e"
REGISTRY_CACHES=(
  "shopflow-cache-docker https://registry-1.docker.io 5060"
  "shopflow-cache-quay https://quay.io 5061"
  "shopflow-cache-ghcr https://ghcr.io 5062"
  "shopflow-cache-k8s https://registry.k8s.io 5063"
)
