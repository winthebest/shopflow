#!/usr/bin/env bash
# Compare the orders created in a time window across Postgres, bronze and (optionally) k6's ack log, read-only. Built for
# game day 2 (killing the shop-db primary): what CDC missed, what the failover lost, what silver will show stale.
#   only in Postgres           CDC never delivered the order to bronze (a gap)
#   only in bronze, deleted    deleted in Postgres and bronze saw the delete (expected)
#   only in bronze, not deleted committed on the old primary, delivered by CDC, then lost by the asynchronous failover
#   r without c                created while Debezium re-snapshotted: in bronze, but not in Flink's KPI (it counts c)
#   status differs             silver shows bronze's state (latest_state prefers streamed c/u over r), not Postgres'
#   acked, not in Postgres     k6 got a 201 for it and the order is gone (with --acks)
# The window is on created_at (Postgres' clock); --acks filters k6's acked_at (the laptop's clock) on the same window.
# Bronze is read in one CDC epoch: --epoch, else the one the sink stamps on new rows (Secret kafka/cdc-epoch). The epoch
#   silver reads (latest in meta.cdc_epochs with a completed snapshot) is printed too; they differ only while a
#   re-snapshot into a new epoch is in progress. Debezium's own snapshot after a lost offset keeps the epoch.
# Also printed: bronze.orders rows with _op = 'r' ingested since --from, the bronze-side proof of such a snapshot.
# Reads Postgres as shop_app and bronze through Trino as `exporter` (catalog lake_ro); passwords go to the pods on stdin.
# Writes pg.tsv, bronze.tsv and one file per finding into --out; prints the counts.
# Run it once CDC has caught up (cdc-lag green, or a minute after the load stops): an order changed in the last seconds
#   would otherwise show as a status difference.
# Usage: scripts/sre-cdc-gap.sh --from RFC3339 --to RFC3339 [--context CTX] [--epoch N] [--acks acks.jsonl] [--out DIR]
set -uo pipefail

CTX="${KUBE_CONTEXT:-k3d-sf-main}"
FROM="" TO="" EPOCH="" ACKS=""
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/out/cdc-gap-$(date -u +%Y%m%dT%H%M%SZ)"
usage() { echo "usage: $0 --from RFC3339 --to RFC3339 [--context CTX] [--epoch N] [--acks FILE] [--out DIR]" >&2; exit 2; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) CTX="${2:?}"; shift ;;
    --from) FROM="${2:?}"; shift ;;
    --to) TO="${2:?}"; shift ;;
    --epoch) EPOCH="${2:?}"; shift ;;
    --acks) ACKS="${2:?}"; shift ;;
    --out) OUT="${2:?}"; shift ;;
    *) usage ;;
  esac
  shift
done
[[ -n "$FROM" && -n "$TO" ]] || usage
[[ -z "$ACKS" || -r "$ACKS" ]] || { echo "cannot read $ACKS" >&2; exit 2; }
# Both bounds as UTC "YYYY-MM-DD HH:MM:SS" (Trino literal) and epoch seconds (ack filter).
utc() { date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$1" "$2" 2>/dev/null || date -u -d "$1" "$2" 2>/dev/null; }
A="$(utc "$FROM" '+%Y-%m-%d %H:%M:%S')" B="$(utc "$TO" '+%Y-%m-%d %H:%M:%S')"
A_S="$(utc "$FROM" +%s)" B_S="$(utc "$TO" +%s)"
[[ -n "$A" && -n "$B" ]] || { echo "--from/--to must be RFC 3339 UTC, e.g. 2026-10-12T08:00:00Z" >&2; exit 2; }
mkdir -p "$OUT" || exit 1

kc() { kubectl --context "$CTX" "$@"; }
secret_value() { kc -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }
# psql as shop_app on the CNPG primary (re-resolved: after a failover the label moves); SQL on stdin; tab-separated.
psql_shop() {
  local pod
  pod="$(kc -n shop get pod -l cnpg.io/cluster=shop-db,cnpg.io/instanceRole=primary -o name | head -1)"
  { secret_value shop shop-db-app password; echo; cat; } | kc -n shop exec -i "${pod#pod/}" -c postgres -- sh -c \
    'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U shop_app -d shop -v ON_ERROR_STOP=1 -qAt -F ,' \
    | tr , '\t'
}
# One Trino query as `exporter` on catalog lake_ro, TSV without header.
trino_ro() {
  local pod
  pod="$(kc -n lakehouse get pod -l app.kubernetes.io/name=trino,app.kubernetes.io/component=coordinator -o name | head -1)"
  # shellcheck disable=SC2016 # expanded by the pod's shell: the password from stdin, the query as $1
  secret_value lakehouse trino-exporter password | kc -n lakehouse exec -i "${pod#pod/}" -- sh -c \
    'IFS= read -r TRINO_PASSWORD; export TRINO_PASSWORD; exec trino --server https://localhost:8443 --insecure \
      --user exporter --password --catalog lake_ro --output-format TSV --execute "$1"' sh "$1"
}

stamped="$(secret_value kafka cdc-epoch epoch)"
read_by_silver="$(psql_shop <<< "SELECT max(epoch) FROM meta.cdc_epochs WHERE snapshot_completed_at IS NOT NULL;")"
[[ -n "$EPOCH" ]] || EPOCH="$stamped"
[[ "$EPOCH" =~ ^[0-9]+$ ]] || { echo "no CDC epoch (Secret kafka/cdc-epoch); pass --epoch" >&2; exit 2; }
echo "orders created in [$A, $B) UTC — context $CTX, bronze read in epoch $EPOCH, output $OUT"
echo "epoch stamped on new rows: $stamped; epoch silver reads: ${read_by_silver:-none}$([[ "$stamped" != "$read_by_silver" ]] && echo ' (differ: a re-snapshot into a new epoch is in progress)')"

# id, status — sorted as text, for join(1).
psql_shop <<SQL | sort -k1,1 > "$OUT/pg.tsv" || { echo "Postgres query failed" >&2; exit 1; }
SELECT id, status FROM orders WHERE created_at >= '$A+00' AND created_at < '$B+00' ORDER BY id;
SQL
# id, seen_c, seen_r, status as silver will rank it, deleted. Ids come from the window on rows other than deletes (a
# delete carries only its key: Debezium fills created_at with a zero value under the default replica identity), then
# every row of those ids is ranked exactly like latest_state: snapshot rows last, then the highest _lsn.
trino_ro "WITH window_ids AS (
    SELECT DISTINCT id FROM bronze.orders
    WHERE _cdc_epoch = $EPOCH AND _op <> 'd'
      AND created_at >= TIMESTAMP '$A UTC' AND created_at < TIMESTAMP '$B UTC'
  ), ranked AS (
    SELECT b.id, b._op, b.status,
      row_number() OVER (PARTITION BY b.id ORDER BY (b._op <> 'r') DESC, b._lsn DESC) AS rk
    FROM bronze.orders b JOIN window_ids w ON b.id = w.id
    WHERE b._cdc_epoch = $EPOCH
  )
  SELECT id, bool_or(_op = 'c'), bool_or(_op = 'r'),
    max(CASE WHEN rk = 1 THEN status END), max(CASE WHEN rk = 1 THEN _op END) = 'd'
  FROM ranked
  GROUP BY id" | sort -k1,1 > "$OUT/bronze.tsv" || { echo "Trino query failed" >&2; exit 1; }

resnapshot_rows="$(trino_ro "SELECT count(*) FROM bronze.orders WHERE _op = 'r' AND _ingested_at >= TIMESTAMP '$A UTC'")"

T=$'\t'
join -t "$T" -v 1 "$OUT/pg.tsv" "$OUT/bronze.tsv" > "$OUT/only-in-postgres.tsv"
# Not in Postgres: bronze's latest row is a delete (expected), or the order was lost (id, seen_c, seen_r, status, deleted).
join -t "$T" -v 2 "$OUT/pg.tsv" "$OUT/bronze.tsv" > "$OUT/only-in-bronze.tsv"
awk -F "$T" '$5 == "true"' "$OUT/only-in-bronze.tsv" > "$OUT/deleted-in-postgres.tsv"
awk -F "$T" '$5 != "true"' "$OUT/only-in-bronze.tsv" > "$OUT/lost-by-failover.tsv"
# Joined: id, pg_status, seen_c, seen_r, bronze_status, deleted.
join -t "$T" "$OUT/pg.tsv" "$OUT/bronze.tsv" > "$OUT/both.tsv"
awk -F "$T" '$3 == "false" && $4 == "true"' "$OUT/both.tsv" > "$OUT/r-without-c.tsv"
awk -F "$T" '$2 != $5 || $6 == "true"' "$OUT/both.tsv" > "$OUT/status-differs.tsv"
rows() { wc -l < "$1" | tr -d ' '; }
printf '%-28s %s\n' "in Postgres" "$(rows "$OUT/pg.tsv")" "in bronze" "$(rows "$OUT/bronze.tsv")" \
  "only in Postgres (CDC gap)" "$(rows "$OUT/only-in-postgres.tsv")" \
  "deleted (bronze saw it)" "$(rows "$OUT/deleted-in-postgres.tsv")" \
  "lost by failover" "$(rows "$OUT/lost-by-failover.tsv")" \
  "r without c (KPI misses)" "$(rows "$OUT/r-without-c.tsv")" \
  "status differs (silver)" "$(rows "$OUT/status-differs.tsv")" \
  "r rows ingested since --from" "${resnapshot_rows:-?} (non-zero: Debezium snapshotted again)"

if [[ -n "$ACKS" ]]; then
  # k6's console output: one {"ack": {...}} per acknowledged checkout; acked_at in UTC with milliseconds.
  jq -R -r --argjson a "$A_S" --argjson b "$B_S" 'fromjson? | .ack? // empty
    | select((.acked_at | sub("\\.[0-9]+"; "") | fromdateiso8601) as $t | $t >= $a and $t < $b) | .order_id' \
    "$ACKS" | sort -u > "$OUT/acked.txt"
  # Acked in the window but absent from Postgres. An order acked just after $B may have been created before it; the
  # Postgres side is re-read without the window for exactly those ids.
  join -v 1 "$OUT/acked.txt" <(cut -f1 "$OUT/pg.tsv") > "$OUT/acked-outside-window.txt"
  if [[ -s "$OUT/acked-outside-window.txt" ]]; then
    ids="$(paste -s -d, "$OUT/acked-outside-window.txt")"
    psql_shop <<< "SELECT id FROM orders WHERE id IN ($ids);" | sort > "$OUT/acked-found-elsewhere.txt"
    join -v 1 "$OUT/acked-outside-window.txt" "$OUT/acked-found-elsewhere.txt" > "$OUT/acked-not-in-postgres.txt"
  else
    : > "$OUT/acked-not-in-postgres.txt"
  fi
  printf '%-28s %s\n' "acked by k6" "$(rows "$OUT/acked.txt")" \
    "acked, not in Postgres" "$(rows "$OUT/acked-not-in-postgres.txt")"
fi
