#!/usr/bin/env bash
# CPU per checkout of two revisions of the shop services, interleaved (docs/perf-baseline.md, 2026-10-13).
#
# Each revision is extracted with `git archive` into a temporary directory and synced from its own lock. The benchmark
# test of the CURRENT checkout (services/orders/tests/integration/test_bench_checkout.py) is copied into both, so a
# revision that predates it is measured the same way; both need the integration fixtures `seeded_db` and `shop_client`.
# Every repetition runs, in this order: <old> without an Idempotency-Key, <new> without a key, <new> with a fresh key
# per checkout. Only ratios within one repetition are compared, because the host's background load drifts between
# repetitions. The control is GET /products (unchanged code): its ratio must stay within ±5 %, else add repetitions.
#
# Needs Docker (one Postgres container per run, via testcontainers): never during a measurement window. On macOS the
# run is refused unless the Mac is on AC power with its lid open, and fails if it slept (scripts/sre-host-guard.sh).
#
# Usage: scripts/app-bench-checkout-cpu.sh <old-rev> <new-rev>      e.g. c35f922 2372d7c
#   REPS (7), BENCH_N (2000 checkouts and 2000 product reads per run), BENCH_WARMUP (200).
#   Results in out/app-bench-checkout-<UTC>/: bench.jsonl (one JSON line per run), a log per run, summary.txt.
set -euo pipefail

cd "$(dirname "$0")/.."
root="$PWD"

[[ $# -eq 2 ]] || { echo "usage: $0 <old-rev> <new-rev>" >&2; exit 2; }
for rev in "$1" "$2"; do
  git rev-parse --verify --quiet "$rev^{commit}" > /dev/null || { echo "unknown revision: $rev" >&2; exit 2; }
done
REPS="${REPS:-7}"
export BENCH_N="${BENCH_N:-2000}" BENCH_WARMUP="${BENCH_WARMUP:-200}"
test_file=services/orders/tests/integration/test_bench_checkout.py
out="$root/out/app-bench-checkout-$(date -u +%Y%m%dT%H%M%SZ)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

start="$(date +%s)"
scripts/sre-host-guard.sh check "$start"

prepare() { # <side> <rev>: the revision's tree with this checkout's benchmark test, synced from its own lock
  mkdir -p "$work/$1"
  git archive "$2" | tar -x -C "$work/$1"
  mkdir -p "$work/$1/$(dirname "$test_file")"
  cp "$test_file" "$work/$1/$test_file"
  (cd "$work/$1" && uv sync --frozen --all-packages --quiet)
  echo "$1 = $2 ($(git rev-parse "$2"))" | tee -a "$out/revisions.txt"
}
prepare old "$1"
prepare new "$2"

run() { # <side> <key: 0|1> <label>
  echo "$(date -u +%H:%M:%SZ) $3 (load $(sysctl -n vm.loadavg 2> /dev/null || cut -d' ' -f1-3 /proc/loadavg))"
  # Keep only the import mode from the archive's pytest options: an older revision does not know the `bench` marker
  # (--strict-markers), and the current one deselects it by default.
  if ! (cd "$work/$1" && BENCH_KEY="$2" BENCH_LABEL="$3" BENCH_OUT="$out/bench.jsonl" \
    uv run --frozen pytest -o addopts=--import-mode=importlib -p no:cacheprovider -q "$test_file") \
    > "$out/$3.log" 2>&1; then
    echo "run $3 failed, see $out/$3.log" >&2
    exit 1
  fi
}
for rep in $(seq 1 "$REPS"); do
  run old 0 "old-nokey-$rep"
  run new 0 "new-nokey-$rep"
  run new 1 "new-key-$rep"
done

scripts/sre-host-guard.sh check "$start" # a sleep during the runs invalidates them

python3 - "$out/bench.jsonl" << 'EOF' | tee "$out/summary.txt"
import json, statistics, sys

runs = {}
for line in open(sys.argv[1]):
    r = json.loads(line)
    config, rep = r["label"].rsplit("-", 1)
    runs.setdefault(config, {})[rep] = r
base = runs["old-nokey"]
print(f"{'config':<10} {'checkout CPU ms':>16} {'/products CPU ms':>17} {'SQL/checkout':>13} {'tx/checkout':>12}")
for config in ("old-nokey", "new-nokey", "new-key"):
    rs = list(runs[config].values())
    mean = lambda k: statistics.mean(r[k] for r in rs)
    print(f"{config:<10} {mean('checkout_cpu_ms'):>16.3f} {mean('products_cpu_ms'):>17.3f}"
          f" {mean('statements_per_checkout'):>13.1f} {mean('transactions_per_checkout'):>12.1f}")
print("\nratio to old in the same repetition: median (range)")
for config in ("new-nokey", "new-key"):
    reps = sorted(set(base) & set(runs[config]))
    for metric, name in (("checkout_cpu_ms", "checkout"), ("products_cpu_ms", "/products control")):
        xs = [runs[config][rep][metric] / base[rep][metric] for rep in reps]
        print(f"  {config:<9} {name:<17} {statistics.median(xs):.3f} ({min(xs):.3f}-{max(xs):.3f}), n={len(xs)}")
EOF
echo "results: $out"
