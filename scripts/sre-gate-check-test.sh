#!/usr/bin/env bash
# Tests for scripts/sre-gate-check.sh with a fake kubectl and a fake Prometheus (run by `scripts/sre-check.sh scripts`).
# The fake kubectl knows one context, like a kubeconfig with one cluster: any other context is an error. Two cases
# used to pass on nothing (a copy of the script without slo/, a spec without service or SLOs); two pin which context
# the check reaches (the default k3d-sf-main, or KUBE_CONTEXT). The rest set what Prometheus answers per case
# (scenario): a failing shop-db exporter query, as measured on a cluster (cnpg_errors_total constant at 1, so
# increase() over it is 0 and a check on increase would pass on it), the Airflow dag_run series with profile batch, and
# the standby's copy of debezium_shop (psql through the fake kubectl).
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
  # shop-db: one instance unless $FAKE_DIR/shopdb.json says otherwise; standby names in $FAKE_DIR/standbys; what psql
  # prints on each standby in $FAKE_DIR/slot-<pod> ("ERROR..." makes psql fail).
  "-n shop get cluster.postgresql.cnpg.io shop-db -o json") cat "$FAKE_DIR/shopdb.json" 2>/dev/null || echo '{"spec": {"instances": 1}}' ;;
  "-n shop get pod -l cnpg.io/cluster=shop-db,cnpg.io/instanceRole=replica -o jsonpath={.items[*].metadata.name}")
    printf '%s' "$(cat "$FAKE_DIR/standbys" 2>/dev/null)" ;;
  "-n shop exec "*" -c postgres -- psql -d shop -qAtX -c SELECT failover, synced FROM pg_replication_slots WHERE slot_name = 'debezium_shop'")
    f="$FAKE_DIR/slot-${args[3]}"; [[ -f "$f" ]] || { echo "Error from server (NotFound): pods \"${args[3]}\" not found" >&2; exit 1; }
    [[ "$(cat "$f")" == ERROR* ]] && { echo "psql: error: connection to server on socket failed" >&2; exit 2; }
    printf '%s' "$(cat "$f")" ;;
  *) echo "kubectl (fake): unexpected arguments: ${args[*]}" >&2; exit 1 ;;
esac
FAKE
chmod +x "$TMP/bin/kubectl"
# Prometheus with one healthy target, no alerts, no failing exporter query, profile data/batch down, and two SLI
# series per SLO query. $FAKE_DIR/scenario.json overrides that: [[query substring, [[labels, value], ...]], ...], the
# first match wins. As in PromQL, a query ending in "> 0" drops the samples that are 0.
cat > "$TMP/prom.py" <<'FAKE'
import http.server, json, os, time, urllib.parse
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query).get("query", [""])[0]
        one = lambda v: [{"metric": {}, "value": [time.time(), v]}]
        with open(os.path.join(os.environ["FAKE_DIR"], "scenario.json")) as f: scenario = json.load(f)
        hit = next((r for k, r in scenario if k in q), None)
        if u.path.endswith("/status/buildinfo"): d = {"version": "fake"}
        elif u.path.endswith("/rules"): d = {"groups": []}
        elif hit is not None:
            res = [{"metric": m, "value": [time.time(), v]} for m, v in hit]
            if q.rstrip().endswith("> 0"): res = [r for r in res if float(r["value"][1]) > 0]
            d = {"resultType": "vector", "result": res}
        elif any(s in q for s in ("up == 0", "ALERTS", "lakehouse", "airflow", "freshness-exporter", "cnpg_errors_total")): d = {"resultType": "vector", "result": []}
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
expect() {  # expect NAME WANT_EXIT WANT_OUTPUT_LINES -- command...  (every line of WANT_OUTPUT_LINES must appear)
  local name="$1" want_rc="$2" want_out="$3" out rc ok=1 line
  shift 4
  out="$("$@" 2>&1)"; rc=$?
  while IFS= read -r line; do [[ "$out" == *"$line"* ]] || ok=0; done <<<"$want_out"
  if [[ $rc -eq $want_rc && $ok -eq 1 ]]; then echo "PASS $name"
  else echo "FAIL $name: exit $rc (want $want_rc, output containing '$want_out'), output: $out" >&2; failed=1; fi
}
scenario() { printf '%s\n' "$1" > "$TMP/scenario.json"; }
scenario '[]'
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

gate=("$TMP/with-slo/scripts/sre-gate-check.sh" --context k3d-test)
expect "no failing exporter query passes" 0 "PASS  shop-db exporter queries" -- "${gate[@]}"
# The shape measured when the exporter could not read Airflow's dag_run: 1 while the query fails, never counting up.
denied='{"errorUserQueries": "airflow_dag_run on db airflow: ERROR: permission denied for table dag_run (SQLSTATE 42501)", "namespace": "shop", "pod": "shop-db-1"}'
scenario "[[\"increase(cnpg_errors_total\", [[$denied, \"0\"]]], [\"rate(cnpg_errors_total\", [[$denied, \"0\"]]],
  [\"cnpg_errors_total\", [[$denied, \"1\"]]]]"
expect "a failing exporter query fails, named with its error" 1 $'FAIL  shop-db exporter queries\nshop-db-1: airflow_dag_run on db airflow: ERROR: permission denied for table dag_run' -- "${gate[@]}"

batch() {  # batch AGE_SECONDS [DAG_RUN_SERIES]: profile batch up for that long, with or without the dag_run series
  local series='[]'
  [[ -n "${2:-}" ]] && series='[[{"dag_id": "dbt_build", "namespace": "shop", "pod": "shop-db-1"}, "0"]]'
  scenario "[[\"cnpg_airflow_dag_run_last_failed\", $series], [\"namespace=\\\"airflow\\\"\", [[{}, \"$1\"]]]]"
}
batch 3600; expect "dag_run series not required in batch's first 2 hours" 0 "SKIP  Airflow dag_run series" -- "${gate[@]}"
batch 10800; expect "batch up 3h without the dag_run series fails" 1 "FAIL  Airflow dag_run series" -- "${gate[@]}"
batch 10800 with-series; expect "batch up 3h with the dag_run series passes" 0 "PASS  Airflow dag_run series" -- "${gate[@]}"

data() {  # data EXPORTER_AGE_SECONDS: profile data up for 2h, the freshness exporter started that long ago
  scenario "[[\"kube_namespace_status_phase{namespace=\\\"lakehouse\\\"\", [[{}, \"1\"]]],
    [\"kube_namespace_created{namespace=\\\"lakehouse\\\"\", [[{}, \"7200\"]]], [\"process_start_time_seconds\", [[{}, \"$1\"]]]]"
}
# Past the SLI rules' 900 s but before their first evaluations: no CDC series yet, by design.
data 960; expect "exporter 960 s old: CDC SLI not expected yet" 0 $'SKIP  SLO cdc has SLI data\nSKIP  CDC heartbeat staleness series' -- "${gate[@]}"
data 1080; expect "exporter 1080 s old: CDC SLI counted" 0 $'PASS  SLO cdc has SLI data\nPASS  CDC heartbeat staleness series' -- "${gate[@]}"

standby() {  # standby INSTANCES SYNC SLOT: shop-db with that many instances and slot sync; what psql shows on shop-db-2
  printf '{"spec": {"instances": %s, "replicationSlots": {"highAvailability": {"synchronizeLogicalDecoding": %s}}}}\n' "$1" "$2" > "$TMP/shopdb.json"
  echo shop-db-2 > "$TMP/standbys"; printf '%s' "$3" > "$TMP/slot-shop-db-2"
}
data 7200
expect "one shop-db instance: no standby slot to check" 0 "SKIP  CDC slot on the standby" -- "${gate[@]}"
standby 2 true 't|t'; expect "standby holds a synced failover copy" 0 "PASS  CDC slot on the standby" -- "${gate[@]}"
standby 2 true ''; expect "standby without the copy fails" 1 $'FAIL  CDC slot on the standby\nshop-db-2: no copy of debezium_shop' -- "${gate[@]}"
standby 2 true 't|f'; expect "a copy not synced fails" 1 "shop-db-2: failover|synced = t|f" -- "${gate[@]}"
standby 2 true 'ERROR'; expect "a failed read is a FAIL, not 'no slot'" 1 "shop-db-2: cannot read pg_replication_slots (psql: error" -- "${gate[@]}"
standby 2 false ''; expect "slot sync off with 2 instances warns" 0 "WARN  CDC slot on the standby" -- "${gate[@]}"
rm -f "$TMP/shopdb.json" "$TMP/standbys" "$TMP/slot-shop-db-2"
scenario '[]'

[[ $failed -eq 0 ]] && echo "sre-gate-check: all tests passed"
exit $failed
