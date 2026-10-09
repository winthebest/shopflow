#!/usr/bin/env bash
# Delete a k3d cluster and its local registry. Environment: CLUSTER (default sf-main).
set -euo pipefail

# shellcheck source=scripts/k3d-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/k3d-lib.sh"

require docker k3d

if cluster_exists; then
  log "deleting cluster (also removes its kubeconfig context)"
  k3d cluster delete "$CLUSTER"
else
  log "no cluster named $CLUSTER"
fi

# A registry made with --registry-create goes away with its cluster; clean up one left by an interrupted run.
if docker container inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
  log "deleting leftover registry $REGISTRY_NAME"
  k3d registry delete "$REGISTRY_NAME"
fi
