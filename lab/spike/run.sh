#!/usr/bin/env bash
# Boundary and tag spikes on a fresh lab stack with the spike overlay (lab/stack.sh up spike). Starts from a reset
# (the stack's volumes and credentials are deleted), stops the stack at the end (KEEP=1 leaves it running).
# Evidence goes to $EVIDENCE (default out/lab-spike/<UTC time>): boundary.{txt,json}, tags.json, mem.tsv.
# Needs docker compose, uv, openssl, htpasswd, jq.
set -euo pipefail

cd "$(dirname "$0")/../.."
EVIDENCE="${EVIDENCE:-out/lab-spike/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$EVIDENCE"

lab/stack.sh reset
lab/stack.sh up spike

# Memory of every container every 5 s while the probes run.
(while :; do lab/stack.sh mem | sed "s/^/$(date -u +%H:%M:%S)\t/" >> "$EVIDENCE/mem.tsv"; sleep 5; done) &
sampler=$!
cleanup() {
  kill "$sampler" 2> /dev/null || true
  wait "$sampler" 2> /dev/null || true
  if [[ "${KEEP:-0}" != 1 ]]; then lab/stack.sh down > /dev/null 2>&1 || true; fi
}
trap cleanup EXIT

echo "== boundary: every operation through every layer catalog"
uv run --quiet lab/spike/boundary-probe.py --json "$EVIDENCE/boundary.json" | tee "$EVIDENCE/boundary.txt"

echo "== tags: expire_snapshots against tags with and without max_ref_age_ms"
lab/stack.sh compose run --rm -T setup sh -c 'pip install --quiet --root-user-action=ignore --disable-pip-version-check \
  "pyiceberg==0.12.0" "trino==0.340.0" && python -I /lab/spike/tag-probe.py' | tee "$EVIDENCE/tags.json"

echo "== evidence in $EVIDENCE; peak memory (MiB) per container and of the whole stack:"
lab/stack.sh mem-peak "$EVIDENCE/mem.tsv"
