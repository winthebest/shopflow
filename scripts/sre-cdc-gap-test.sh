#!/usr/bin/env bash
# Tests for scripts/sre-cdc-gap.sh with a fake kubectl (run by `scripts/sre-check.sh scripts`). The fake knows one
# context, answers the Secrets, the primary and Trino pods, and the psql and Trino execs: it checks that each password
# arrives on stdin and that every query reads the expected epoch, and it rejects any other call. What Postgres and bronze
# hold comes from $FAKE_DIR/scenario.json, per table: "pg" rows [id, status] and "bronze" rows [id, seen_c, seen_r,
# status, deleted], as the script's own queries return them.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
export PATH="$TMP/bin:$PATH" FAKE_DIR="$TMP"

cat > "$TMP/bin/kubectl" <<'FAKE'
#!/usr/bin/env bash
exec python3 -I "$FAKE_DIR/kubectl.py" "$@"
FAKE
chmod +x "$TMP/bin/kubectl"
cat > "$TMP/kubectl.py" <<'FAKE'
import base64, json, os, re, sys
args = sys.argv[1:]
def die(msg):
    sys.stderr.write(msg + "\n"); sys.exit(1)
if args[:2] != ["--context", "k3d-test"]:
    die(f'error: context "{args[1] if len(args) > 1 else ""}" does not exist')
a = args[2:]
d = os.environ["FAKE_DIR"]
scenario = json.load(open(os.path.join(d, "scenario.json")))
secrets = {("kafka", "cdc-epoch", "epoch"): "7", ("shop", "shop-db-app", "password"): "pg-test-password",
           ("lakehouse", "trino-exporter", "password"): "trino-test-password"}
fail = os.environ.get("FAKE_FAIL", "")
if len(a) == 7 and a[0] == "-n" and a[2:4] == ["get", "secret"] and a[5] == "-o":
    key = re.fullmatch(r"jsonpath=\{\.data\.(\w+)\}", a[6])
    v = key and secrets.get((a[1], a[4], key.group(1)))
    if not v: die(f'Error from server (NotFound): secrets "{a[4]}" not found')
    print(base64.b64encode(v.encode()).decode(), end="")
elif a == ["-n", "shop", "get", "pod", "-l", "cnpg.io/cluster=shop-db,cnpg.io/instanceRole=primary", "-o", "name"]:
    print("pod/shop-db-1")
elif a == ["-n", "lakehouse", "get", "pod", "-l", "app.kubernetes.io/name=trino,app.kubernetes.io/component=coordinator", "-o", "name"]:
    print("pod/trino-coordinator-0")
elif a[:9] == ["-n", "shop", "exec", "-i", "shop-db-1", "-c", "postgres", "--", "sh"] and len(a) == 11:
    stdin = sys.stdin.read().split("\n", 1)
    if stdin[0] != "pg-test-password": die("psql: password authentication failed")
    sql = " ".join(stdin[1].split())
    if "meta.cdc_epochs" in sql: print("7")
    elif m := re.fullmatch(r"SELECT id, (status|'') FROM (\w+) WHERE created_at >= '[0-9: -]+\+00' AND created_at < '[0-9: -]+\+00' ORDER BY id;", sql):
        if fail == "pg:" + m.group(2): die("ERROR: canceling statement due to conflict with recovery")
        for i, s in scenario.get(m.group(2), {}).get("pg", []): print(f"{i},{s}")
    elif m := re.fullmatch(r"SELECT id FROM orders WHERE id IN \(([0-9,]+)\);", sql):
        known = {str(i) for i, _ in scenario.get("orders", {}).get("pg", [])} | set(map(str, scenario.get("orders_elsewhere", [])))
        print("\n".join(i for i in m.group(1).split(",") if i in known))
    else: die(f"fake psql: unexpected SQL {sql}")
elif a[:7] == ["-n", "lakehouse", "exec", "-i", "trino-coordinator-0", "--", "sh"] and len(a) == 11 and a[9] == "sh":
    if sys.stdin.read().strip() != "trino-test-password": die("trino: Invalid credentials")
    q = " ".join(a[10].split())
    if m := re.search(r"FROM bronze\.(\w+) WHERE _cdc_epoch = (\d+) AND _op <> 'd'", q):
        if m.group(2) != "7": die(f"fake trino: epoch {m.group(2)}, want 7")
        if fail == "trino:" + m.group(1): die("Query failed: Table 'lake_ro.bronze." + m.group(1) + "' does not exist")
        for r in scenario.get(m.group(1), {}).get("bronze", []):
            print("\t".join(str(x).lower() if isinstance(x, bool) else str(x) for x in r))
    elif q.startswith("SELECT count(*) FROM bronze.orders WHERE _op = 'r'"): print(scenario.get("r_rows", 0))
    else: die(f"fake trino: unexpected query {q}")
else:
    die(f"kubectl (fake): unexpected arguments: {a}")
FAKE

failed=0
expect() {  # expect NAME WANT_EXIT WANT_OUTPUT_LINES -- command...  (every line of WANT_OUTPUT_LINES must appear)
  local name="$1" want_rc="$2" want_out="$3" out rc ok=1 line
  shift 4
  out="$("$@" 2>&1)"; rc=$?
  while IFS= read -r line; do [[ "$out" == *"$line"* ]] || ok=0; done <<<"$want_out"
  [[ "$out" == *test-password* ]] && { ok=0; echo "  a password reached the output" >&2; }
  if [[ $rc -eq $want_rc && $ok -eq 1 ]]; then echo "PASS $name"
  else echo "FAIL $name: exit $rc (want $want_rc, output containing '$want_out'), output: $out" >&2; failed=1; fi
}
scenario() { printf '%s\n' "$1" > "$TMP/scenario.json"; }
gap=("$REPO/scripts/sre-cdc-gap.sh" --context k3d-test --from 2026-10-14T05:09:17Z --to 2026-10-14T05:39:17Z --out "$TMP/out")
# Every table complete: orders 1-3, payments 11-12 (one still pending), shipments 21.
clean='"orders": {"pg": [[1, "paid"], [2, "paid"], [3, "failed"]],
    "bronze": [[1, true, false, "paid", false], [2, true, false, "paid", false], [3, true, false, "failed", false]]},
  "payments": {"pg": [[11, "succeeded"], [12, "pending"]],
    "bronze": [[11, true, false, "succeeded", false], [12, true, false, "pending", false]]},
  "shipments": {"pg": [[21, ""]], "bronze": [[21, true, false, "", false]]}'

scenario "{$clean}"
expect "every table complete" 0 $'only in Postgres (CDC gap)   0\npayments created in the window\n  only in Postgres (CDC gap)   0\n  status differs (silver)      0\nshipments created in the window\n  in Postgres                  1\n  in bronze                    1' -- "${gap[@]}"
if [[ -s "$TMP/out/payments/pg.tsv" && -s "$TMP/out/shipments/bronze.tsv" && -s "$TMP/out/pg.tsv" ]]; then echo "PASS files per table"
else echo "FAIL files per table: $(find "$TMP/out" -type f | sort)" >&2; failed=1; fi

# A payment and a shipment written but never delivered by CDC: the hazard window's other writers.
scenario "{$clean, \"payments\": {\"pg\": [[11, \"succeeded\"], [12, \"pending\"]], \"bronze\": [[11, true, false, \"succeeded\", false]]},
  \"shipments\": {\"pg\": [[21, \"\"], [22, \"\"]], \"bronze\": [[21, true, false, \"\", false]]}}"
expect "a payment and a shipment missing from bronze are CDC gaps" 0 $'payments created in the window\n  in Postgres                  2\n  in bronze                    1\n  only in Postgres (CDC gap)   1' -- "${gap[@]}"
if [[ "$(cut -f1 "$TMP/out/payments/only-in-postgres.tsv")" == 12 && "$(cut -f1 "$TMP/out/shipments/only-in-postgres.tsv")" == 22 ]]; then
  echo "PASS the missing ids are listed"
else echo "FAIL the missing ids are listed" >&2; failed=1; fi

# An update CDC missed: Postgres has the payment succeeded, silver will still show it pending.
scenario "{$clean, \"payments\": {\"pg\": [[11, \"succeeded\"]], \"bronze\": [[11, true, false, \"pending\", false]]}}"
expect "a payment update CDC missed is a status difference" 0 $'payments created in the window\n  status differs (silver)      1' -- "${gap[@]}"

# Delivered to bronze, then gone from Postgres without a delete: lost by the failover.
scenario "{$clean, \"payments\": {\"pg\": [], \"bronze\": [[11, true, false, \"succeeded\", false], [12, true, false, \"failed\", true]]}}"
expect "a payment in bronze only is lost, a deleted one is not" 0 $'  lost by failover             1\n  deleted (bronze saw it)      1' -- "${gap[@]}"

# Orders keep their own findings and output: a snapshot-only order is a KPI miss.
scenario "{$clean, \"orders\": {\"pg\": [[1, \"paid\"]], \"bronze\": [[1, false, true, \"paid\", false]]}, \"r_rows\": 1}"
expect "orders: a snapshot-only order is a KPI miss" 0 $'r without c (KPI misses)     1\nr rows ingested since --from 1 (non-zero: Debezium snapshotted again)' -- "${gap[@]}"

# A failed query never reads as "no gap".
scenario "{$clean}"
expect "a failed Trino query on payments fails the run" 1 "Trino query on bronze.payments failed" -- env FAKE_FAIL=trino:payments "${gap[@]}"
expect "a failed Postgres query on shipments fails the run" 1 "Postgres query on shipments failed" -- env FAKE_FAIL=pg:shipments "${gap[@]}"

# k6 acks: an acked order absent from Postgres, inside or outside the window.
printf '%s\n' '{"ack":{"order_id":2,"acked_at":"2026-10-14T05:20:00.100Z"}}' '{"ack":{"order_id":9,"acked_at":"2026-10-14T05:21:00.200Z"}}' \
  'not json' > "$TMP/acks.jsonl"
expect "an acked order missing from Postgres is reported" 0 $'acked by k6                  2\nacked, not in Postgres       1' -- \
  "${gap[@]}" --acks "$TMP/acks.jsonl"

[[ $failed -eq 0 ]] && echo "sre-cdc-gap: all tests passed"
exit $failed
