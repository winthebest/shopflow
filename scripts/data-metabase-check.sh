#!/usr/bin/env bash
# Check Metabase on a running cluster (profile bi, docs/adr/0416), read-only except for writes that must be refused:
# 1. Trino as user metabase: reading gold works; reading silver, any query on catalog lake, CREATE TABLE and INSERT
#    are denied (lake_ro is a READ_ONLY catalog and rules.json gives metabase SELECT on gold only).
# 2. Metabase's API as the provisioned admin: the dashboard from git exists and every question on it returns rows
#    (needs gold to have data: profile batch, or one manual dbt build).
# Passwords go from the Secrets through stdin or curl --config, never through arguments or output.
# Usage: CLUSTER=<name> scripts/data-metabase-check.sh   (exit 1 if any check fails)
set -uo pipefail

CONTEXT="k3d-${CLUSTER:?CLUSTER is required}"
PORT="${METABASE_LOCAL_PORT:-19300}"
DASHBOARD="Shopflow sales"
K=(kubectl --context "$CONTEXT")
failures=0

pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failures=$((failures + 1)); }

# trino_as_metabase <sql>: runs one statement in the Trino coordinator as user metabase; output on stdout+stderr.
trino_as_metabase() {
  local pod
  pod="$("${K[@]}" -n lakehouse get pod -l app.kubernetes.io/name=trino,app.kubernetes.io/component=coordinator \
    -o name | head -1)"
  # shellcheck disable=SC2016 # expanded inside the pod
  "${K[@]}" -n lakehouse get secret trino-metabase -o jsonpath='{.data.password}' | base64 -d \
    | "${K[@]}" -n lakehouse exec -i "$pod" -- sh -c \
      'IFS= read -r TRINO_PASSWORD; export TRINO_PASSWORD; exec trino --server https://localhost:8443 --insecure \
        --user metabase --password --output-format TSV --execute "$1"' sh "$1" 2>&1 | grep -v WARNING
}

# expect_allowed / expect_denied <label> <sql>
expect_allowed() {
  local out
  if out="$(trino_as_metabase "$2")" && ! grep -qiE "error|denied|failed" <<< "$out"; then
    pass "$1 (rows: $(wc -l <<< "$out" | tr -d ' '))"
  else
    fail "$1: $(head -1 <<< "$out")"
  fi
}
expect_denied() {
  local out
  out="$(trino_as_metabase "$2")"
  if grep -qiE "access denied|read-only|not allowed|cannot be (used|accessed)" <<< "$out"; then
    pass "$1 denied ($(grep -oiE "access denied[^:]*|read-only[^.]*|not allowed" <<< "$out" | head -1))"
  else
    fail "$1 was NOT denied: $(head -1 <<< "$out")"
  fi
}

echo "== Trino as user metabase"
expect_allowed "read lake_ro.gold" "SELECT count(*) FROM lake_ro.gold.mart_daily_revenue"
expect_denied "read lake_ro.silver" "SELECT count(*) FROM lake_ro.silver.orders"
expect_denied "read catalog lake" "SELECT count(*) FROM lake.gold.mart_daily_revenue"
expect_denied "CREATE TABLE in lake_ro.gold" "CREATE TABLE lake_ro.gold.metabase_write_probe (x integer)"
expect_denied "INSERT into lake_ro.gold" \
  "INSERT INTO lake_ro.gold.mart_daily_revenue SELECT * FROM lake_ro.gold.mart_daily_revenue WHERE false"

echo "== Metabase API (dashboard from git)"
"${K[@]}" -n metabase port-forward svc/metabase "$PORT:3000" > /dev/null 2>&1 &
PF=$!
trap 'kill "$PF" 2> /dev/null || true' EXIT
for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/api/health" > /dev/null && break; sleep 1; done

# mb <method> <path> [json]: authenticated call; the session header is passed through curl's config on stdin.
session="$(
  {
    printf 'url = "http://127.0.0.1:%s/api/session"\n' "$PORT"
    printf 'header = "Content-Type: application/json"\n'
    printf 'data = "%s"\n' "$(jq -nc --arg u "$("${K[@]}" -n metabase get secret metabase-admin -o jsonpath='{.data.email}' | base64 -d)" \
      --rawfile p <("${K[@]}" -n metabase get secret metabase-admin -o jsonpath='{.data.password}' | base64 -d) \
      '{username: $u, password: $p}' | sed 's/\\/\\\\/g; s/"/\\"/g')"
  } | curl -sf --config - | jq -r '.id // empty'
)"
if [[ -z "$session" ]]; then
  fail "admin login (Secret metabase-admin) refused"
  echo "$failures check(s) failed"
  exit 1
fi
mb() {
  {
    printf 'url = "http://127.0.0.1:%s%s"\n' "$PORT" "$2"
    printf 'request = "%s"\n' "$1"
    printf 'header = "X-Metabase-Session: %s"\n' "$session"
    printf 'header = "Content-Type: application/json"\n'
    [[ -n "${3:-}" ]] && printf 'data = "%s"\n' "$(sed 's/\\/\\\\/g; s/"/\\"/g' <<< "$3")"
  } | curl -sf --config -
}

collection="$(mb GET /api/collection | jq -r '.[] | select(.name == "Shopflow (from git)" and (.archived | not)) | .id' | head -1)"
dashboard="$(mb GET "/api/collection/$collection/items?models=dashboard" \
  | jq -r --arg n "$DASHBOARD" '.data[] | select(.name == $n) | .id' | head -1)"
if [[ -z "$dashboard" ]]; then
  fail "dashboard \"$DASHBOARD\" not found (did the metabase-provision Job run?)"
else
  pass "dashboard \"$DASHBOARD\" (id $dashboard)"
  while IFS=$'\t' read -r card name; do
    result="$(mb POST "/api/card/$card/query" '{}')"
    status="$(jq -r '.status // "error"' <<< "$result")"
    rows="$(jq -r '.row_count // 0' <<< "$result")"
    if [[ "$status" == completed && "$rows" -gt 0 ]]; then
      pass "question \"$name\": $rows rows"
    else
      fail "question \"$name\": status $status, $rows rows $(jq -r '.error // empty' <<< "$result" | head -c 200)"
    fi
  done < <(mb GET "/api/dashboard/$dashboard" | jq -r '.dashcards[] | select(.card_id) | [.card_id, .card.name] | @tsv')
fi

echo "$failures check(s) failed"
((failures == 0))
