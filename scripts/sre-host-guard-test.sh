#!/usr/bin/env bash
# Tests for scripts/sre-host-guard.sh with fake pmset and ioreg (run by `scripts/sre-check.sh scripts`, on macOS or
# Linux). The fakes accept only the exact arguments the guard uses, so a changed call fails here instead of misreading
# the real tools.
set -uo pipefail
GUARD="$(cd "$(dirname "$0")" && pwd)/sre-host-guard.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export TZ=UTC HOST_GUARD_OS=Darwin PATH="$TMP/bin:$PATH" FAKE_DIR="$TMP"
mkdir -p "$TMP/bin"

cat > "$TMP/bin/pmset" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "-g batt") cat "$FAKE_DIR/batt" ;;
  "-g log") cat "$FAKE_DIR/log" ;;
  *) echo "pmset: unexpected arguments: $*" >&2; exit 1 ;;
esac
EOF
cat > "$TMP/bin/ioreg" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == "-r -k AppleClamshellState -d 4" ]] || { echo "ioreg: unexpected arguments: $*" >&2; exit 1; }
printf '+-o IOPMrootDomain  <class IOPMrootDomain>\n  |   "AppleClamshellState" = %s\n' "$(cat "$FAKE_DIR/lid")"
# Like the real tool, thousands of lines follow: a reader that stops at the first match makes ioreg die of SIGPIPE.
for i in $(seq 1 20000); do echo "  |   \"IOPowerManagement$i\" = {}"; done
EOF
chmod +x "$TMP/bin/pmset" "$TMP/bin/ioreg"

failed=0
expect() {  # expect NAME WANT_EXIT WANT_OUTPUT_SUBSTRING -- command...
  local name="$1" want_rc="$2" want_out="$3" out rc
  shift 4
  out="$("$@" 2>&1)"; rc=$?
  if [[ $rc -eq $want_rc && "$out" == *"$want_out"* ]]; then echo "PASS $name"
  else echo "FAIL $name: exit $rc (want $want_rc), output: $out" >&2; failed=1; fi
}
unix() { date -u -d "$1" +%s 2>/dev/null || date -j -u -f '%Y-%m-%d %H:%M:%S' "$1" +%s; }
batt() { printf "Now drawing from '%s'\n -InternalBattery-0 (id=1)\t80%%; charging\n" "$1" > "$TMP/batt"; }

batt 'AC Power'; echo No > "$TMP/lid"
expect "AC power and lid open pass" 0 "AC power, lid open" -- "$GUARD" preflight
batt 'Battery Power'
expect "battery fails" 1 "on battery" -- "$GUARD" preflight
batt 'AC Power'; echo Yes > "$TMP/lid"
expect "closed lid fails" 1 "lid closed" -- "$GUARD" preflight
batt 'Battery Power'
expect "battery and closed lid report both" 1 "lid closed" -- "$GUARD" preflight

# Two sleeps (10:00 and 10:20). The other lines mention sleep but are not sleep events.
printf '%s\n' \
  "2026-10-12 09:59:58 +0000 Assertions          	PID 101(powerd) Released PreventUserIdleSystemSleep \"x\"" \
  "2026-10-12 10:00:00 +0000 Sleep               	Entering Sleep state due to 'Clamshell Sleep':TCPKeepAlive=active" \
  "2026-10-12 10:05:00 +0000 PM Client Acks      	Delays to Sleep notifications: [AirPort configd plug-in is slow]" \
  "2026-10-12 10:10:00 +0000 DarkWake            	DarkWake from Deep Idle [CDNP] : due to SMC" \
  "2026-10-12 10:12:00 +0000 Kernel Client Acks  	Delays to Sleep notifications: [Port-USB-C driver is slow]" \
  "2026-10-12 10:20:00 +0000 Sleep               	Entering Sleep state due to 'Maintenance Sleep'" \
  "2026-10-12 10:30:00 +0000 Wake                	Wake from Deep Idle [CDNP] : due to UserActivity" > "$TMP/log"
expect "sleeps over the whole log" 0 "2" -- "$GUARD" sleeps "$(unix '2026-10-12 09:00:00')" "$(unix '2026-10-12 11:00:00')"
expect "a sleep at SINCE counts" 0 "1" -- "$GUARD" sleeps "$(unix '2026-10-12 10:00:00')" "$(unix '2026-10-12 10:15:00')"
expect "a sleep at UNTIL does not" 0 "0" -- "$GUARD" sleeps "$(unix '2026-10-12 10:01:00')" "$(unix '2026-10-12 10:20:00')"
expect "awake since the last sleep" 0 "0" -- "$GUARD" awake-since "$(unix '2026-10-12 10:21:00')"
expect "slept since" 1 "2" -- "$GUARD" awake-since "$(unix '2026-10-12 09:00:00')"
batt 'AC Power'; echo No > "$TMP/lid"
expect "check: fit and awake since 10:21" 0 "no sleep since" -- "$GUARD" check "$(unix '2026-10-12 10:21:00')"
expect "check: slept in the window" 1 "slept 2 time(s)" -- "$GUARD" check "$(unix '2026-10-12 09:00:00')"
batt 'Battery Power'
expect "check: on battery stops before counting" 1 "on battery" -- "$GUARD" check "$(unix '2026-10-12 10:21:00')"
echo Yes > "$TMP/lid"
lines="$("$GUARD" preflight 2>&1 >/dev/null)"
if [[ $(wc -l <<< "$lines") -eq 1 && "$lines" == *battery*lid* ]]; then echo "PASS both reasons on one line"
else echo "FAIL both reasons on one line: $lines" >&2; failed=1; fi

HOST_GUARD_OS=Linux expect "not macOS: preflight passes" 0 "nothing to check" -- "$GUARD" preflight
HOST_GUARD_OS=Linux expect "not macOS: no sleeps" 0 "0" -- "$GUARD" sleeps 0
expect "bad time is a usage error" 2 "usage" -- "$GUARD" sleeps yesterday
expect "unknown command is a usage error" 2 "usage" -- "$GUARD" status

[[ $failed -eq 0 ]] && echo "sre-host-guard: all tests passed"
exit $failed
