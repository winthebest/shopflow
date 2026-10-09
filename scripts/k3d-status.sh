#!/usr/bin/env bash
# Show what a local cluster is running: nodes, Argo CD Applications, the public route, memory use.
# Environment: CLUSTER (default sf-main).
set -euo pipefail

# shellcheck source=scripts/k3d-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/k3d-lib.sh"

require docker k3d kubectl

cluster_exists || die "no cluster named $CLUSTER (make up CLUSTER=$CLUSTER)"

echo "== nodes"
kc get nodes -o wide
echo
echo "== Argo CD applications"
kc -n argocd get applications.argoproj.io \
  -o custom-columns='NAME:.metadata.name,WAVE:.metadata.annotations.argocd\.argoproj\.io/sync-wave,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision' \
  2>/dev/null || echo "(Argo CD not installed)"
echo
echo "== public routes (only shop is exposed; admin UIs use port-forward)"
kc get gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io -A 2>/dev/null \
  || echo "(Gateway API not installed yet)"
echo "   https://shop.127.0.0.1.sslip.io:$HTTPS_PORT"
echo
echo "== container memory"
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep "k3d-$CLUSTER-" || true
