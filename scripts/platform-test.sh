#!/usr/bin/env bash
# Offline tests for scripts/k3d-up.sh (used by platform-ci): each case sources the script (main does not run when
# sourced) and stubs the commands around the function under test, so no cluster or Docker is needed.
# Usage: scripts/platform-test.sh   (exit 1 on the first failing case)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILS=0

# run_case <name> <expected exit> <expected output fragment> <bash snippet run after sourcing k3d-up.sh>
run_case() {
  local name="$1" want_rc="$2" want_out="$3" snippet="$4" out rc
  out="$(cd "$ROOT_DIR" && CLUSTER=sf-test API_PORT=6599 HTTPS_PORT=9499 CALLS="$WORK/calls" bash -c '
    set -euo pipefail
    source scripts/k3d-up.sh
    log() { printf "[log] %s\n" "$*" >&2; }
    die() { printf "[die] %s\n" "$*" >&2; exit 1; }
    '"$snippet" 2>&1)" && rc=0 || rc=$?
  if [[ "$rc" == "$want_rc" && "$out" == *"$want_out"* ]]; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'FAIL  %s (exit %s, want %s; output: %s)\n' "$name" "$rc" "$want_rc" "$out"
    FAILS=$((FAILS + 1))
  fi
}

# A fake cdc-epoch.sh: records its arguments, succeeds or fails on demand (FAKE_WAIT=ok|fail).
mkdir -p "$WORK/repo/scripts"
cat > "$WORK/repo/scripts/cdc-epoch.sh" <<'EOF'
#!/usr/bin/env bash
echo "cdc-epoch.sh $*" >> "$CALLS"
[[ "$1" != wait || "${FAKE_WAIT:-ok}" == ok ]]
EOF
chmod +x "$WORK/repo/scripts/cdc-epoch.sh"
FAKE="ROOT_DIR=$WORK/repo; : > \$CALLS"

# wait_cdc_snapshot: profile data records the epoch's snapshot after the apps; other profile sets skip it.
run_case "no data: cdc-epoch wait is not called" 0 "calls=0" \
  "$FAKE; PROFILES=core,obs-lite; WAIT_TIMEOUT=900; wait_cdc_snapshot; echo calls=\$(wc -l < \$CALLS | tr -d ' ')"
run_case "data: wait --epoch E --timeout 900" 0 "cdc-epoch.sh wait --epoch 1791629476 --timeout 900" \
  "$FAKE; PROFILES=core,obs-lite,data; WAIT_TIMEOUT=900; CDC_EPOCH=1791629476; wait_cdc_snapshot; cat \$CALLS"
run_case "data: CDC_WAIT_TIMEOUT overrides the timeout" 0 "--timeout 60" \
  "$FAKE; PROFILES=core,obs-lite,data; WAIT_TIMEOUT=900; CDC_EPOCH=7; CDC_WAIT_TIMEOUT=60; wait_cdc_snapshot; cat \$CALLS"
run_case "data: a wait timeout fails make up with the epoch in the message" 1 "CDC epoch 7: snapshot not recorded" \
  "$FAKE; export FAKE_WAIT=fail; PROFILES=core,obs-lite,data; WAIT_TIMEOUT=900; CDC_EPOCH=7; wait_cdc_snapshot"
run_case "data, WAIT_TIMEOUT=0: skipped with a warning" 0 "warning: WAIT_TIMEOUT=0 skips the CDC snapshot record" \
  "$FAKE; PROFILES=core,obs-lite,data; WAIT_TIMEOUT=0; CDC_EPOCH=7; wait_cdc_snapshot; [[ ! -s \$CALLS ]]"

# main: the snapshot wait runs after every app is healthy, and the ready line reports both times.
run_case "main: ensure, apps, then the snapshot wait" 0 "order=ensure,apps,snapshot" \
  "PROFILES=core,obs-lite,data; order=();
   preflight() { :; }; create_cluster() { :; }; install_argocd() { :; }; apply_root_apps() { :; }
   start_cdc_epoch() { order+=(ensure); }; wait_for_apps() { order+=(apps); }; wait_cdc_snapshot() { order+=(snapshot); }
   main; IFS=,; echo \"order=\${order[*]}\""
run_case "main: the ready line splits apps and CDC snapshot time" 0 "CDC snapshot" \
  "PROFILES=core,obs-lite,data; preflight() { :; }; create_cluster() { :; }; install_argocd() { :; }
   apply_root_apps() { :; }; start_cdc_epoch() { :; }; wait_for_apps() { :; }; wait_cdc_snapshot() { :; }; main"

# local_params: the failover game day's shop-db params reach platform-root-apps.sh.
run_case "PG_INSTANCES / PG_LOGICAL_SLOT_SYNC become session params" 0 \
  "--param pg.instances=2 --param pg.logicalSlotSync=off" \
  "PROFILES=core; PG_INSTANCES=2; PG_LOGICAL_SLOT_SYNC=off; local_params; echo \"\${PARAMS[*]}\""

# shop-db chart: session params arrive as strings (or null when unset); slot sync only with more than one instance.
chart_case() { # name, expected "instances sync", helm args...
  local name="$1" want="$2" got; shift 2
  got="$(cd "$ROOT_DIR" && helm template shop-db deploy/charts/shop-db -n shop -f deploy/charts/shop-db/values.yaml \
    -f deploy/charts/shop-db/values-local.yaml "$@" 2>&1 | yq -N 'select(.kind == "Cluster")
      | (.spec.instances | tostring) + " " + ((.spec.replicationSlots.highAvailability.synchronizeLogicalDecoding // false) | tostring)
      + " " + (.spec.postgresql.parameters.sync_replication_slots // "-") + " " + (.spec.postgresql.parameters.hot_standby_feedback // "-")')"
  if [[ "$got" == "$want" ]]; then printf 'PASS  %s\n' "$name"; else
    printf 'FAIL  %s (got: %s, want: %s)\n' "$name" "$got" "$want"; FAILS=$((FAILS + 1)); fi
}
chart_case "shop-db: default is 1 instance, no slot sync" "1 false - -"
chart_case "shop-db: unset params (null) keep the defaults" "1 false - -" --set instances=null --set logicalSlotSync=null
chart_case "shop-db: instances '2' turns on slot sync" "2 true on on" --set-string instances=2
chart_case "shop-db: instances '2', logicalSlotSync 'off'" "2 false - -" --set-string instances=2 --set-string logicalSlotSync=off
# CNPG's webhook prepends its default-queries ConfigMap; the chart must list it first too, or Argo CD never syncs.
monitoring="$(cd "$ROOT_DIR" && helm template shop-db deploy/charts/shop-db -n shop -f deploy/charts/shop-db/values.yaml \
  -f deploy/charts/shop-db/values-local.yaml | yq -N 'select(.kind == "Cluster")
    | .spec.monitoring.customQueriesConfigMap | map(.name + "/" + .key) | join(",")')"
if [[ "$monitoring" == "cnpg-default-monitoring/queries,shop-db-monitoring/queries.yaml" ]]; then
  printf 'PASS  shop-db: exporter queries match what CNPG writes (defaults first)\n'
else
  printf 'FAIL  shop-db: exporter queries (got: %s)\n' "$monitoring"; FAILS=$((FAILS + 1))
fi

# k3d-lib.sh: every cluster of docs/contracts/environment.md gets its ports without overrides.
for row in "sf-main 6550 9443 5050 18080" "sf-platform 6551 8443 5051 18081" "sf-sre 6552 8444 5052 18082" \
  "sf-data 6553 8445 5053 18083" "sf-app 6554 8446 5054 18084" "sf-cloud 6555 8447 5055 18085"; do
  read -r cluster api https registry argocd <<<"$row"
  # shellcheck disable=SC2016  # the inner shell expands the variables after sourcing k3d-lib.sh
  out="$(cd "$ROOT_DIR" && env -u API_PORT -u HTTPS_PORT -u REGISTRY_PORT -u ARGOCD_UI_PORT CLUSTER="$cluster" \
    bash -c 'source scripts/k3d-lib.sh; echo "$API_PORT $HTTPS_PORT $REGISTRY_PORT $ARGOCD_UI_PORT"' 2>&1)" || true
  if [[ "$out" == "$api $https $registry $argocd" ]]; then
    printf 'PASS  ports of %s\n' "$cluster"
  else
    printf 'FAIL  ports of %s (got: %s, want: %s %s %s %s)\n' "$cluster" "$out" "$api" "$https" "$registry" "$argocd"
    FAILS=$((FAILS + 1))
  fi
done

((FAILS == 0)) || { echo "platform-test: $FAILS case(s) failed" >&2; exit 1; }
echo "platform-test: all cases pass"
