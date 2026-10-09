#!/usr/bin/env bash
# Delete the shared pull-through image caches and their data (they survive `make down` on purpose).
# Clusters created with the caches keep working afterwards: containerd falls back to the upstream registries.
set -euo pipefail

# shellcheck source=scripts/k3d-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/k3d-lib.sh"

require docker k3d

for entry in "${REGISTRY_CACHES[@]}"; do
  read -r name _ _ <<<"$entry"
  if docker container inspect "k3d-$name" >/dev/null 2>&1; then
    log "deleting cache k3d-$name"
    k3d registry delete "k3d-$name"
  fi
  if docker volume inspect "$name" >/dev/null 2>&1; then
    log "deleting cache data volume $name"
    docker volume rm "$name" >/dev/null
  fi
done
