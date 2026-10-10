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

((FAILS == 0)) || { echo "platform-test: $FAILS case(s) failed" >&2; exit 1; }
echo "platform-test: all cases pass"
