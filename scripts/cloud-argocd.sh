#!/usr/bin/env bash
# Argo CD admin access on the AWS session cluster. The UI is never exposed through the NLB (ADR 0205).
#   cloud-argocd.sh ui        port-forward https://localhost:18090 (foreground; Ctrl-C stops it)
#   cloud-argocd.sh password  copy the admin password (user admin) from SSM to the clipboard; never printed
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  parse_common_args "$@"
  case "${ARGS[0]:-}" in
    ui)
      require_cmds kubectl
      log "Argo CD UI on https://localhost:${ARGOCD_UI_PORT:-18090} (self-signed certificate; user admin)"
      exec kubectl --kubeconfig "$KUBECONFIG_FILE" -n argocd port-forward --address 127.0.0.1 svc/argocd-server "${ARGOCD_UI_PORT:-18090}:443"
      ;;
    password)
      require_cmds aws pbcopy
      aws_ ssm get-parameter --name "$SSM_ARGOCD_ADMIN" --with-decryption --query Parameter.Value --output text |
        tr -d '\n' | pbcopy
      log "copied the Argo CD admin password to the clipboard (user admin)"
      ;;
    *)
      usage >&2
      die "usage: $0 ui|password"
      ;;
  esac
}

main "$@"
