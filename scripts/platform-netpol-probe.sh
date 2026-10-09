#!/usr/bin/env bash
# Prove the NetworkPolicies of docs/adr/0208 on a running cluster: positive and negative TCP probes between the
# namespaces of the core, obs/obs-lite and data profiles. Each probe is a short-lived pod that carries the labels of
# the source workload (policies select by labels), so no workload image needs a shell or netcat.
#
#   scripts/platform-netpol-probe.sh                 run the probes, print a table, exit 1 on any mismatch
#   scripts/platform-netpol-probe.sh --apply REV     first apply apps network-policies-{obs,data} at Git revision REV
#   scripts/platform-netpol-probe.sh --remove        delete those two apps and their policies (namespaces stay)
#
# A probe whose source or target namespace/pod does not exist is reported as SKIP, not as a failure.
# Cluster: CLUSTER (default sf-main) selects context k3d-<CLUSTER>; KUBE_CONTEXT overrides it.
# Needs: kubectl, yq, jq. Probe image: the orders image running in namespace shop (Python, non-root).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-k3d-${CLUSTER:-sf-main}}"
APPS=(network-policies-obs network-policies-data)
REPO_URL="https://github.com/winthebest/shopflow.git"

kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }
log() { printf '[netpol-probe] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

# Source namespace | source pod labels | target namespace | target pod selector | port | expected
PROBES=(
  # core (PR #75)
  "shop|app.kubernetes.io/name=gateway|shop|app.kubernetes.io/name=orders|8001|open"
  "shop|app.kubernetes.io/name=gateway|shop|cnpg.io/cluster=shop-db|5432|blocked"
  "shop|app.kubernetes.io/name=orders|observability|app.kubernetes.io/instance=otel-gateway|4317|open"
  "default|app.kubernetes.io/name=probe|shop|app.kubernetes.io/name=gateway|8000|blocked"
  # data -> Postgres and lakehouse
  "kafka|strimzi.io/kind=KafkaConnect|shop|cnpg.io/cluster=shop-db|5432|open"
  "kafka|strimzi.io/kind=KafkaConnect|lakehouse|app.kubernetes.io/name=polaris|8181|open"
  "kafka|strimzi.io/kind=KafkaConnect|lakehouse|app.kubernetes.io/name=seaweedfs|8333|open"
  "kafka|app.kubernetes.io/name=probe|shop|cnpg.io/cluster=shop-db|5432|blocked"
  "kafka|strimzi.io/kind=KafkaConnect|shop|app.kubernetes.io/name=gateway|8000|blocked"
  "lakehouse|app.kubernetes.io/name=trino|shop|cnpg.io/cluster=shop-db|5432|open"
  "lakehouse|app.kubernetes.io/name=polaris|shop|cnpg.io/cluster=shop-db|5432|open"
  "lakehouse|app.kubernetes.io/name=probe|shop|cnpg.io/cluster=shop-db|5432|blocked"
  "lakehouse|app.kubernetes.io/name=probe|lakehouse|app.kubernetes.io/name=trino|8443|open"
  "default|app.kubernetes.io/name=probe|lakehouse|app.kubernetes.io/name=polaris|8181|blocked"
  # observability
  "observability|app.kubernetes.io/name=prometheus|shop|cnpg.io/cluster=shop-db|9187|open"
  "observability|app.kubernetes.io/name=prometheus|kafka|strimzi.io/kind=KafkaConnect|9404|open"
  "observability|app.kubernetes.io/name=prometheus|lakehouse|app.kubernetes.io/name=freshness-exporter|8080|open"
  "observability|app.kubernetes.io/name=grafana|shop|cnpg.io/cluster=shop-db|5432|open"
  "observability|app.kubernetes.io/name=grafana|kafka|strimzi.io/kind=KafkaConnect|9404|blocked"
  "default|app.kubernetes.io/name=probe|observability|app.kubernetes.io/instance=otel-gateway|4317|blocked"
)
# Egress to the internet must be closed for workloads (1.1.1.1:443 as the stand-in).
INTERNET_PROBES=(
  "shop|app.kubernetes.io/name=orders"
  "lakehouse|app.kubernetes.io/name=trino"
  "kafka|strimzi.io/kind=KafkaConnect"
)

apply_apps() {
  local rev="$1" app
  for app in "${APPS[@]}"; do
    REPO="$REPO_URL" REV="$rev" yq '(.spec.sources[] | select(.repoURL == strenv(REPO)) | .targetRevision) = strenv(REV)' \
      "$ROOT_DIR/deploy/argocd/apps/$app/application.yaml" | kc apply -f - >/dev/null
    log "applied $app @ $rev"
  done
  local deadline=$((SECONDS + 300))
  for app in "${APPS[@]}"; do
    until [[ "$(kc -n argocd get applications.argoproj.io "$app" -o jsonpath='{.status.sync.status}/{.status.health.status}' 2>/dev/null)" == "Synced/Healthy" ]]; do
      ((SECONDS < deadline)) || die "$app not Synced/Healthy after 300s"
      sleep 5
    done
  done
  log "both apps Synced/Healthy"
}

remove_apps() {
  local app
  for app in "${APPS[@]}"; do
    kc -n argocd get applications.argoproj.io "$app" >/dev/null 2>&1 || continue
    # The resources finalizer makes Argo CD delete the policies; the Namespaces keep Delete=false.
    kc -n argocd patch applications.argoproj.io "$app" --type merge \
      -p '{"metadata":{"finalizers":["resources-finalizer.argocd.argoproj.io"]}}' >/dev/null
    kc -n argocd delete applications.argoproj.io "$app" --wait=true --timeout=180s >/dev/null
    log "removed $app"
  done
}

probe_image() {
  kc -n shop get deploy orders -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null \
    || die "no orders Deployment in namespace shop (the probe image comes from it)"
}

# probe <ns> <labels> <host> <port> -> open | blocked | error. The pod passes PSA restricted (namespace shop
# enforces it) and mounts no token. It waits 8s before connecting: the k3s policy controller adds a new pod to
# its rule sets a few seconds after the pod starts (measured: until then the pod is not yet isolated), and it
# retries a few times so a slow first SYN is not read as "blocked".
probe() {
  local ns="$1" labels="$2" host="$3" port="$4" name result
  name="netpol-probe-$RANDOM"
  jq -n --arg name "$name" --arg ns "$ns" --arg labels "$labels" --arg image "$IMAGE" --arg host "$host" --arg port "$port" '{
    apiVersion: "v1", kind: "Pod",
    metadata: {name: $name, namespace: $ns, labels: ($labels | split(",") | map(split("=") | {(.[0]): .[1]}) | add)},
    spec: {
      restartPolicy: "Never", automountServiceAccountToken: false,
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, seccompProfile: {type: "RuntimeDefault"}},
      containers: [{
        name: "probe", image: $image,
        command: ["python3", "-c",
          ("import socket, time\ntime.sleep(8)\nr = \"blocked\"\nfor _ in range(4):\n    s = socket.socket(); s.settimeout(2)\n    if s.connect_ex((\"" + $host + "\", " + $port + ")) == 0:\n        r = \"open\"; break\n    s.close(); time.sleep(1)\nprint(r)")],
        securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}},
        resources: {requests: {cpu: "10m", memory: "32Mi"}, limits: {memory: "64Mi"}}
      }]
    }}' | kc apply -f - >/dev/null 2>&1 || { echo error; return; }
  if kc -n "$ns" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=90s >/dev/null 2>&1; then
    result="$(kc -n "$ns" logs "$name" 2>/dev/null | tail -1)"
  else
    result=error
  fi
  kc -n "$ns" delete pod "$name" --wait=false >/dev/null 2>&1 || true
  echo "${result:-error}"
}

ns_exists() { kc get namespace "$1" >/dev/null 2>&1; }

run_probes() {
  IMAGE="$(probe_image)"
  local entry src labels dst selector port want ip got status fails=0
  printf '%-14s %-38s %-14s %-40s %-5s %-8s %-8s %s\n' FROM LABELS TO TARGET PORT WANT GOT RESULT
  for entry in "${PROBES[@]}"; do
    IFS='|' read -r src labels dst selector port want <<<"$entry"
    ip=""
    if ns_exists "$src" && ns_exists "$dst"; then
      ip="$(kc -n "$dst" get pods -l "$selector" --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
    fi
    if [[ -z "$ip" ]]; then
      got="-" status=SKIP
    else
      got="$(probe "$src" "$labels" "$ip" "$port")"
      if [[ "$got" == "$want" ]]; then status=PASS; else status=FAIL; fails=$((fails + 1)); fi
    fi
    printf '%-14s %-38s %-14s %-40s %-5s %-8s %-8s %s\n' "$src" "$labels" "$dst" "$selector" "$port" "$want" "$got" "$status"
  done
  for entry in "${INTERNET_PROBES[@]}"; do
    IFS='|' read -r src labels <<<"$entry"
    if ns_exists "$src"; then
      got="$(probe "$src" "$labels" 1.1.1.1 443)"
      if [[ "$got" == blocked ]]; then status=PASS; else status=FAIL; fails=$((fails + 1)); fi
    else
      got="-" status=SKIP
    fi
    printf '%-14s %-38s %-14s %-40s %-5s %-8s %-8s %s\n' "$src" "$labels" internet 1.1.1.1 443 blocked "$got" "$status"
  done
  ((fails == 0)) || die "$fails probe(s) did not match the policy"
  log "all probes match"
}

case "${1:-}" in
  "") run_probes ;;
  --apply) apply_apps "${2:?--apply needs a Git revision}"; run_probes ;;
  --remove) remove_apps ;;
  *) die "usage: $0 [--apply REV | --remove]" ;;
esac
