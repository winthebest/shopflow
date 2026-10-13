#!/usr/bin/env bash
# Tests for scripts/sre-gate-check.sh with a fake kubectl and a fake Prometheus (run by `scripts/sre-check.sh scripts`).
# The fake kubectl knows one context, like a kubeconfig with one cluster: any other context is an error. Two cases
# used to pass on nothing (a copy of the script without slo/, a spec without service or SLOs); two pin which context
# the check reaches (the default k3d-sf-main, or KUBE_CONTEXT).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
export PATH="$TMP/bin:$PATH" FAKE_DIR="$TMP"

cat > "$TMP/bin/kubectl" <<'FAKE'
#!/usr/bin/env bash
ctx="" args=()
while [[ $# -gt 0 ]]; do case "$1" in --context) ctx="$2"; shift 2 ;; *) args+=("$1"); shift ;; esac; done
[[ "$ctx" == k3d-test ]] || { echo "error: context \"${ctx:-<none>}\" does not exist" >&2; exit 1; }
case "${args[*]}" in
  "get --raw /readyz") echo ok ;;
  "-n observability port-forward svc/kps-prometheus :9090") exec python3 "$FAKE_DIR/prom.py" ;;
  "-n observability get pod tempo-0") echo 'Error from server (NotFound): pods "tempo-0" not found' >&2; exit 1 ;;
  *) echo "kubectl (fake): unexpected arguments: ${args[*]}" >&2; exit 1 ;;
esac
FAKE
chmod +x "$TMP/bin/kubectl"
# Prometheus with one healthy target, no alerts, profile data/batch down, and two SLI series per SLO query.
cat > "$TMP/prom.py" <<'FAKE'
import http.server, json, time, urllib.parse
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query).get("query", [""])[0]
        one = lambda v: [{"metric": {}, "value": [time.time(), v]}]
        if u.path.endswith("/status/buildinfo"): d = {"version": "fake"}
        elif u.path.endswith("/rules"): d = {"groups": []}
        elif any(s in q for s in ("up == 0", "ALERTS", "lakehouse", "airflow", "freshness-exporter")): d = {"resultType": "vector", "result": []}
        elif "slo:sli_error:ratio_rate5m" in q: d = {"resultType": "vector", "result": one("0") * 2}
        elif "restarts_total" in q or "notifications_failed" in q: d = {"resultType": "vector", "result": one("0")}
        else: d = {"resultType": "vector", "result": one("1")}
        b = json.dumps({"status": "success", "data": d}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
s = http.server.HTTPServer(("127.0.0.1", 0), H)
print(f"Forwarding from 127.0.0.1:{s.server_port} -> 9090", flush=True)
s.serve_forever()
FAKE

failed=0
expect() {  # expect NAME WANT_EXIT WANT_OUTPUT_SUBSTRING -- command...
  local name="$1" want_rc="$2" want_out="$3" out rc
  shift 4
  out="$("$@" 2>&1)"; rc=$?
  if [[ $rc -eq $want_rc && "$out" == *"$want_out"* ]]; then echo "PASS $name"
  else echo "FAIL $name: exit $rc (want $want_rc, output containing '$want_out'), output: $out" >&2; failed=1; fi
}
# A copy of the scripts, with or without the SLO specs next to them (the game-day runners copy tools like this).
copy() { mkdir -p "$TMP/$1/scripts"; cp "$REPO/scripts/sre-gate-check.sh" "$REPO/scripts/sre-prom-lib.sh" "$TMP/$1/scripts/"; }
copy with-slo; cp -R "$REPO/slo" "$TMP/with-slo/slo"
copy without-slo
copy bad-slo; mkdir -p "$TMP/bad-slo/slo"; printf 'version: prometheus/v1\nspec: {}\n' > "$TMP/bad-slo/slo/empty.yaml"

expect "specs present: checkout SLI counted" 0 "PASS  SLO checkout has SLI data" -- "$TMP/with-slo/scripts/sre-gate-check.sh" --context k3d-test
expect "no slo/ next to the script fails" 1 "FAIL  SLO specs found" -- "$TMP/without-slo/scripts/sre-gate-check.sh" --context k3d-test
expect "a spec without service or SLOs fails" 1 "FAIL  SLO spec readable" -- "$TMP/bad-slo/scripts/sre-gate-check.sh" --context k3d-test
expect "unknown context cannot reach the cluster" 2 "cannot reach the API server of context k3d-sf-main" -- \
  env -u KUBE_CONTEXT "$TMP/with-slo/scripts/sre-gate-check.sh"
expect "KUBE_CONTEXT selects the context" 0 "context k3d-test" -- env KUBE_CONTEXT=k3d-test "$TMP/with-slo/scripts/sre-gate-check.sh"

[[ $failed -eq 0 ]] && echo "sre-gate-check: all tests passed"
exit $failed
