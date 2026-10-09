#!/usr/bin/env bash
# Argo CD admin access for a local cluster. The UI is never exposed through the Gateway (ADR 0205).
#   k3d-argocd.sh ui        port-forward https://localhost:<ARGOCD_UI_PORT> (foreground; Ctrl-C stops it)
#   k3d-argocd.sh password  copy the admin password (user admin) from SOPS to the clipboard
# Environment: CLUSTER (default sf-main), SOPS_AGE_KEY_FILE (default ~/.config/sops/age/keys.txt).
set -euo pipefail

# shellcheck source=scripts/k3d-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/k3d-lib.sh"

case "${1:-}" in
  ui)
    require kubectl
    log "Argo CD UI on https://localhost:$ARGOCD_UI_PORT (self-signed certificate; user admin)"
    exec kubectl --context "$KUBE_CONTEXT" -n argocd port-forward --address 127.0.0.1 svc/argocd-server "$ARGOCD_UI_PORT:443"
    ;;
  password)
    require sops pbcopy
    export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
    sops -d --extract '["stringData"]["password"]' "$ROOT_DIR/deploy/secrets/local/argocd-admin.enc.yaml" | pbcopy
    log "copied the Argo CD admin password to the clipboard (user admin)"
    ;;
  *)
    die "usage: $0 ui|password"
    ;;
esac
