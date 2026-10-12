#!/usr/bin/env bash
# End of a game day: remove Chaos Mesh completely (docs/runbooks/gameday.md, ADR 0303).
# Order matters:
#   1. delete root-chaos without cascading (any resources finalizer dropped first), so it cannot recreate the
#      chaos-mesh Application;
#   2. delete every chaos-mesh.org object while the controller still runs (it removes their finalizers; once it is
#      gone they would block the CRD deletion forever);
#   3. delete the chaos-mesh Application: its resources finalizer removes the chart (daemon, webhooks, RBAC);
#   4. delete the chaos-mesh.org CRDs: Argo CD applies a Helm chart's crds/ without tracking them, so deleting the
#      Application leaves them (seen in game day 3);
#   5. delete namespace chaos-mesh (the TLS Secrets cert-manager issued are not part of the chart);
#   6. verify nothing is left.
# Kubernetes context: KUBE_CONTEXT if set, otherwise the current context.
set -euo pipefail

kc() { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} "$@"; }
log() { printf '[sre-chaos-off] %s\n' "$*" >&2; }
die() { log "FAIL: $*"; exit 1; }

log "1/6 delete root-chaos (orphan its children)"
if kc -n argocd get applications.argoproj.io root-chaos >/dev/null 2>&1; then
  # A resources finalizer on the root would cascade despite --cascade=orphan (same as platform-root-apps.sh).
  kc -n argocd patch applications.argoproj.io root-chaos --type merge -p '{"metadata":{"finalizers":null}}' >/dev/null
  kc -n argocd delete applications.argoproj.io root-chaos --cascade=orphan --wait=true --timeout=120s >/dev/null
fi

log "2/6 delete chaos experiments while the controller still runs"
kinds="$(kc api-resources --api-group=chaos-mesh.org -o name 2>/dev/null || true)"
for kind in $kinds; do
  kc delete "$kind" --all --all-namespaces --ignore-not-found --wait=true --timeout=120s >/dev/null
done

log "3/6 delete the chaos-mesh Application and wait for its resources"
kc -n argocd delete applications.argoproj.io chaos-mesh --ignore-not-found --wait=true --timeout=300s >/dev/null

log "4/6 delete the chaos-mesh.org CRDs (not tracked by Argo CD)"
crds="$(kc get crd -o name | grep -E '\.chaos-mesh\.org$' || true)"
if [[ -n "$crds" ]]; then
  # shellcheck disable=SC2086 # one argument per CRD
  kc delete $crds --wait=true --timeout=120s >/dev/null
fi

log "5/6 delete namespace chaos-mesh"
kc delete namespace chaos-mesh --ignore-not-found --wait=true --timeout=180s >/dev/null

log "6/6 verify"
left="$(kc api-resources --api-group=chaos-mesh.org -o name 2>/dev/null || true)"
[[ -z "$left" ]] || die "chaos-mesh.org CRDs still served: $(echo "$left" | tr '\n' ' ')"
! kc get namespace chaos-mesh >/dev/null 2>&1 || die "namespace chaos-mesh still exists"
hooks="$(kc get mutatingwebhookconfigurations,validatingwebhookconfigurations -o name | grep -i chaos || true)"
[[ -z "$hooks" ]] || die "chaos webhooks left: $hooks"
log "Chaos Mesh removed: no namespace, CRDs or webhooks left"
