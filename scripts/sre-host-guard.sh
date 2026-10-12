#!/usr/bin/env bash
# Is the laptop fit for a measurement? Shared by every runner that measures on k3d (game days, gate checks, data and app
# slots, restore drills). `caffeinate -dims` keeps an idle Mac awake, but not one on battery with its lid closed
# (clamshell sleep): on 2026-10-12 that froze a game-day cluster for three hours. So a runner checks the host before it
# starts, and a run whose window contains a sleep is invalid.
#   preflight             exit 1 unless the Mac is on AC power and its lid is open (the reason on stderr)
#   sleeps SINCE [UNTIL]  how many times the Mac went to sleep between two Unix times (UNTIL defaults to now), from
#                         pmset's log; dark wakes in between are part of the same sleep
#   awake-since SINCE     exit 1 if it slept since SINCE (prints the count)
# Not macOS: nothing to check; preflight passes and sleeps prints 0, with a note on stderr.
set -uo pipefail

OS="${HOST_GUARD_OS:-$(uname -s)}"   # tests set Darwin to run the macOS paths with fake pmset/ioreg

usage() { echo "usage: $0 preflight | sleeps SINCE [UNTIL] | awake-since SINCE   (Unix seconds)" >&2; exit 2; }
not_macos() { echo "host guard: $OS is not macOS, nothing to check" >&2; }
is_time() { [[ "$1" =~ ^[0-9]+$ ]]; }
# Local "YYYY-MM-DD HH:MM:SS" of a Unix time, the way pmset prints its log (BSD date on macOS, GNU date in CI).
local_time() { date -r "$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$1" '+%Y-%m-%d %H:%M:%S'; }

preflight() {
  [[ "$OS" == Darwin ]] || { not_macos; return 0; }
  local ok=0 batt lid
  # Read each tool's whole output first: `tool | grep -q` stops reading at the first match, the tool dies of SIGPIPE,
  # and pipefail turns that into a failed check (ioreg prints thousands of lines).
  batt="$(pmset -g batt)" lid="$(ioreg -r -k AppleClamshellState -d 4)"
  # First line: "Now drawing from 'AC Power'" or "... 'Battery Power'".
  if ! head -1 <<< "$batt" | grep -q "'AC Power'"; then
    echo "host guard: on battery; plug in the charger (a closed lid on battery sleeps despite caffeinate)" >&2; ok=1
  fi
  if ! grep -q '"AppleClamshellState" = No' <<< "$lid"; then
    echo "host guard: lid closed (or state unknown); open it" >&2; ok=1
  fi
  [[ $ok -eq 0 ]] && echo "host guard: AC power, lid open"
  return $ok
}

# pmset log lines: "2026-10-12 19:27:53 +0700 Sleep  <tab>Entering Sleep state due to ...". Only the event-type
# column counts: other lines mention sleep too ("Delays to Sleep notifications", assertion names).
sleeps() {
  local since="$1" until="${2:-$(date +%s)}"
  if ! is_time "$since" || ! is_time "$until"; then usage; fi
  [[ "$OS" == Darwin ]] || { not_macos; echo 0; return 0; }
  pmset -g log | awk -v a="$(local_time "$since")" -v b="$(local_time "$until")" \
    '$4 == "Sleep" && ($1 " " $2) >= a && ($1 " " $2) < b { n++ } END { print n + 0 }'
}

case "${1:-}" in
  preflight) [[ $# -eq 1 ]] || usage; preflight ;;
  sleeps) [[ $# -eq 2 || $# -eq 3 ]] || usage; sleeps "$2" "${3:-}" ;;
  awake-since)
    [[ $# -eq 2 ]] || usage
    n="$(sleeps "$2")" || exit 2
    echo "$n"
    [[ "$n" == 0 ]] ;;
  *) usage ;;
esac
