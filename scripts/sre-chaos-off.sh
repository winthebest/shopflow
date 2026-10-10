#!/usr/bin/env bash
# End of a game day: remove Chaos Mesh completely (docs/runbooks/gameday.md, ADR 0303).
# Order matters:
#   1. delete root-chaos without cascading, so it cannot recreate the chaos-mesh Application;
#   2. delete every chaos-mesh.org object while the controller still runs (it removes their finalizers; once it is
#      gone they would block the CRD deletion forever);
#   3. delete the chaos-mesh Application: its resources finalizer removes the chart (daemon, webhooks, CRDs);
#   4. verify nothing is left.
# Kubernetes context: KUBE_CONTEXT if set, otherwise the current context.
set -euo pipefail

kc() { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} "$@"; }
log() { printf '[sre-chaos-off] %s\n' "$*" >&2; }
die() { log "FAIL: $*"; exit 1; }

log "1/4 delete root-chaos (orphan its children)"
kc -n argocd delete applications.argoproj.io root-chaos --cascade=orphan --ignore-not-found --wait=true --timeout=120s >/dev/null

log "2/4 delete chaos experiments while the controller still runs"
kinds="$(kc api-resources --api-group=chaos-mesh.org -o name 2>/dev/null || true)"
for kind in $kinds; do
  kc delete "$kind" --all --all-namespaces --ignore-not-found --wait=true --timeout=120s >/dev/null
done

log "3/4 delete the chaos-mesh Application and wait for its resources"
kc -n argocd delete applications.argoproj.io chaos-mesh --ignore-not-found --wait=true --timeout=300s >/dev/null

log "4/4 verify"
left="$(kc api-resources --api-group=chaos-mesh.org -o name 2>/dev/null || true)"
[[ -z "$left" ]] || die "chaos-mesh.org CRDs still served: $(echo "$left" | tr '\n' ' ')"
if kc get namespace chaos-mesh >/dev/null 2>&1; then
  pods="$(kc -n chaos-mesh get pods --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  [[ "$pods" == 0 ]] || die "namespace chaos-mesh still has $pods pod(s)"
fi
hooks="$(kc get mutatingwebhookconfigurations,validatingwebhookconfigurations -o name | grep -i chaos || true)"
[[ -z "$hooks" ]] || die "chaos webhooks left: $hooks"
log "Chaos Mesh removed: no CRDs, daemon or webhooks left"
