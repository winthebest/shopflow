#!/usr/bin/env bash
# Lakehouse lab stack (lab/compose.yml).
#   lab/stack.sh up [spike]   create or start the stack; `spike` adds the lab/spike overlay (OPA, layer-probe catalogs)
#   lab/stack.sh down         stop and remove the containers; volumes and credentials stay
#   lab/stack.sh reset        remove containers, volumes and $LAB_RUN_DIR (every credential)
#   lab/stack.sh base RUN     make RUN the active baseline run: its catalog lab_base_RUN (identity baseline_writer)
#   lab/stack.sh base-drop    no active baseline run (its warehouse and data stay until reset)
#   lab/stack.sh py ZONE -- ARGS  `uv run --project lab ARGS` in a container on the lab network, with ZONE's REST
#                             principal and S3 key (zone exp: lab_exp's), for writers that commit through Polaris
#   lab/stack.sh mem          one `docker stats` sample of the running containers (name, memory, CPU)
#   lab/stack.sh mem-peak F   peak per container and of the stack's sum, from lines "time<TAB>$(lab/stack.sh mem)"
#   lab/stack.sh compose ...  docker compose with this stack's files and env (e.g. `compose logs polaris`)
#
# Credentials are throwaway: generated on the first `up` after a reset into $LAB_RUN_DIR (default out/lab, gitignored,
# mode 0700) and never written to the repository. Tests and spikes read them from there. Why the stack looks like
# this: docs/adr/0417-catalog-scoped-safety-boundary.md.
#
# Zones and identities (lab/trino/rules.json, lab/polaris.json): each Trino catalog has its own Polaris principal and
# S3 key, and each zone has its own Polaris catalog (warehouse) and bucket, so a credential reaches only its zone.
#   lab_exp  harness         warehouse lab_exp       bucket lab-exp       read/write, floors 0s
#   lab_ops  executor        warehouse lab_evalprod  bucket lab-evalprod  read/write, floors 7m
#   lab_ro   agent_planner   warehouse lab_evalprod  bucket lab-evalprod  read only (Trino, Polaris and S3)
#   lab_sbx  executor        warehouse lab_sandbox   bucket lab-sandbox   read/write, floors 7m
#   lab_base_RUN  baseline_writer  warehouse lab_base_RUN  bucket lab-base (s3://lab-base/RUN/warehouse)
#                                                                         read/write, floors 7m; one active run at a time
#   lab_ro, lab_exp_ro, lab_sbx_ro, lab_base_ro_RUN  observer  each zone's warehouse, read only (Trino, Polaris, S3):
#                                                    state captures only; no arm session is built as observer.
#     observer's rules there are catalog `all` with SELECT and OWNERSHIP: file rules allow SHOW CREATE TABLE only
#     to an owner in a catalog with full access. Writes, DDL and procedures stay refused by the READ_ONLY connector.
# Floors are 7 simulated days at the published time scale, 1 simulated day = 1 real minute (lab/src/lab/timescale.py).
# Nobody may set the *_min_retention session properties. Every other catalog is `allow: none` for every identity.
# A baseline run's catalog holds only its own warehouse (Polaris also refuses table locations outside it), so a
# baseline writes freely inside its run and reaches no other run's tables. `base RUN` restarts Trino.
# Each identity sees and cancels only its own queries (rules.json `queries`; without that section Trino lets every
# user view and kill every query).
# The OTel collector takes OTLP on 127.0.0.1:24317 (gRPC) and 24318 (HTTP) and writes $LAB_RUN_DIR/otel/*.jsonl.
# The harness (lab/src/lab/guard.py) refuses the platform's lakehouse: catalogs, warehouse and bucket `lake`.
# Needs docker compose, openssl, htpasswd, jq.
set -euo pipefail

LAB="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(cd "$LAB/.." && pwd -P)"
RUN="${LAB_RUN_DIR:-$REPO/out/lab}"
# The OTel collector runs as the host user: its output files in $RUN/otel belong to whoever started the stack.
LAB_UID="$(id -u)" LAB_GID="$(id -g)"
export LAB_UID LAB_GID
USERS="harness agent_planner executor baseline_writer observer"
BUCKETS="lab-exp,lab-evalprod,lab-sandbox,lab-base"
# S3 identities: name, env prefix in trino.env (empty: not a Trino key), actions.
S3_IDENTITIES="polaris||Read:lab-exp,List:lab-exp,Write:lab-exp,Read:lab-evalprod,List:lab-evalprod,Write:lab-evalprod,Read:lab-sandbox,List:lab-sandbox,Write:lab-sandbox,Read:lab-base,List:lab-base,Write:lab-base
exp|EXP|Read:lab-exp,List:lab-exp,Write:lab-exp
evalprod|EVALPROD|Read:lab-evalprod,List:lab-evalprod,Write:lab-evalprod
evalprod-ro|EVALPROD_RO|Read:lab-evalprod,List:lab-evalprod
exp-ro|EXP_RO|Read:lab-exp,List:lab-exp
sandbox-ro|SANDBOX_RO|Read:lab-sandbox,List:lab-sandbox
sandbox|SANDBOX|Read:lab-sandbox,List:lab-sandbox,Write:lab-sandbox
base|BASE|Read:lab-base,List:lab-base,Write:lab-base
base-ro|BASE_RO|Read:lab-base,List:lab-base"
SPIKE_USERS="spike"
SPIKE_BUCKETS="lab-spike"
SPIKE_S3_IDENTITIES="spike|SPIKE|Read:lab-spike,List:lab-spike,Write:lab-spike
spike-ro|SPIKE_RO|Read:lab-spike,List:lab-spike"

die() { echo "lab/stack.sh: $*" >&2; exit 1; }

# Compose mounts $RUN into containers and `reset` deletes it: an absolute path without `.`/`..` components only.
[[ "$RUN" == /* ]] || die "LAB_RUN_DIR must be an absolute path: $RUN"
case "$RUN/" in */./* | */../*) die "LAB_RUN_DIR must not contain . or .. components: $RUN" ;; esac

secret() { openssl rand -hex 16; }

mode_of_run() { sed -n 's/^LAB_MODE=//p' "$RUN/.env" 2> /dev/null; }

compose() {
  local files=(-f "$LAB/compose.yml")
  if [[ "$(mode_of_run)" == spike ]]; then files+=(-f "$LAB/spike/compose.yml"); fi
  docker compose --project-directory "$LAB" --env-file "$RUN/.env" "${files[@]}" "$@"
}

# First `up` after a reset: every credential, the S3 identities, the catalog role SQL, Trino's password file and TLS.
generate() {
  local mode="$1" users="$USERS" buckets="$BUCKETS" identities="$S3_IDENTITIES"
  if [[ "$mode" == spike ]]; then
    users="$users $SPIKE_USERS"
    buckets="$buckets,$SPIKE_BUCKETS"
    identities="${identities/polaris||/polaris||Read:lab-spike,List:lab-spike,Write:lab-spike,}
$SPIKE_S3_IDENTITIES"
  fi
  (umask 077 && mkdir -p "$RUN")
  chmod 0700 "$RUN"
  mkdir -p "$RUN/initdb" "$RUN/seaweedfs" "$RUN/passwords" "$RUN/trino/tls" "$RUN/trino/catalog"

  local polaris_db_password
  polaris_db_password="$(secret)"
  (
    umask 077
    {
      echo "LAB_MODE=$mode"
      echo "LAB_RUN_DIR=$RUN"
      echo "LAB_BUCKETS=$buckets"
      echo "CATALOG_DB_SUPERUSER_PASSWORD=$(secret)"
      echo "POLARIS_DB_PASSWORD=$polaris_db_password"
      echo "POLARIS_ROOT_SECRET=$(secret)"
      echo "TRINO_INTERNAL_SHARED_SECRET=$(secret)"
    } > "$RUN/.env"
    : > "$RUN/trino.env"
  )

  # The files below are bind-mounted into containers that run as other users: readable inside $RUN (0700).
  cat > "$RUN/initdb/10-catalog.sql" <<SQL
CREATE ROLE polaris LOGIN PASSWORD '$polaris_db_password';
CREATE DATABASE catalog OWNER polaris;
\connect catalog
CREATE SCHEMA polaris_schema AUTHORIZATION polaris;
SQL

  local json="" name prefix actions access key
  while IFS='|' read -r name prefix actions; do
    access="$(secret)" key="$(secret)"
    if [[ "$name" == polaris ]]; then
      printf 'POLARIS_S3_ACCESS_KEY=%s\nPOLARIS_S3_SECRET_KEY=%s\n' "$access" "$key" >> "$RUN/.env"
    else
      printf 'S3_%s_ACCESS_KEY=%s\nS3_%s_SECRET_KEY=%s\n' "$prefix" "$access" "$prefix" "$key" >> "$RUN/trino.env"
    fi
    json="$json$(jq -cn --arg n "$name" --arg a "$access" --arg k "$key" --arg act "$actions" \
      '{name: $n, credentials: [{accessKey: $a, secretKey: $k}], actions: ($act | split(","))}'),"
  done <<< "$identities"
  printf '{"identities": [%s]}\n' "${json%,}" | jq . > "$RUN/seaweedfs/s3.json"

  local user password
  : > "$RUN/trino/password.db"
  for user in $users; do
    password="$(secret)"
    (umask 077 && printf '%s' "$password" > "$RUN/passwords/$user")
    htpasswd -niB -C 10 "$user" <<< "$password" | sed '/^$/d' >> "$RUN/trino/password.db"
  done

  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=lab-trino" \
    -addext "subjectAltName=DNS:localhost,DNS:trino,IP:127.0.0.1" \
    -keyout "$RUN/trino/tls/key.pem" -out "$RUN/trino/tls/cert.pem" 2> /dev/null
  cat "$RUN/trino/tls/key.pem" "$RUN/trino/tls/cert.pem" > "$RUN/trino/tls/trino.pem"
  rm "$RUN/trino/tls/key.pem"

  find "$RUN/initdb" "$RUN/seaweedfs" "$RUN/trino" -type d -exec chmod 0755 {} +
  find "$RUN/initdb" "$RUN/seaweedfs" "$RUN/trino" -type f -exec chmod 0644 {} +
}

active_run() { cat "$RUN/base/active" 2> /dev/null || true; }

# A run directory made by an older version of this script lacks the identities and keys added since: Trino would
# start with unresolved ${ENV:…} catalog properties.
check_credentials() {
  local user name prefix actions
  for user in $USERS; do
    [[ -f "$RUN/passwords/$user" ]] || die "$RUN has no password for $user (made by an older lab/stack.sh): reset it"
  done
  while IFS='|' read -r name prefix actions; do
    [[ -z "$prefix" ]] || grep -q "^S3_${prefix}_ACCESS_KEY=" "$RUN/trino.env" \
      || die "$RUN has no S3 key $name (made by an older lab/stack.sh): reset it"
  done <<< "$S3_IDENTITIES"
}

# Trino's rules and catalogs: the base files, plus the overlay's (its rules go first: the first matching rule wins),
# plus the active baseline run's catalog. The rules name it as lab_base_@RUN@, which matches no catalog until a run is
# active. rules.json is rewritten in place (same inode): Trino re-reads its bind mount every 60 s.
render() {
  local mode="$1" run rules
  run="$(active_run)"
  mkdir -p "$RUN/otel"
  rm -f "$RUN/trino/catalog/"*.properties
  cp "$LAB/trino/etc/catalog/"*.properties "$RUN/trino/catalog/"
  if [[ "$mode" == spike ]]; then
    cp "$LAB/spike/trino/catalog/"*.properties "$RUN/trino/catalog/"
    rules="$(jq -s '.[0] as $o | .[1] | with_entries(.value = (($o[.key] // []) + .value))' \
      "$LAB/spike/trino/rules.json" "$LAB/trino/rules.json")"
  else
    rules="$(cat "$LAB/trino/rules.json")"
  fi
  if [[ -n "$run" ]]; then
    local upper template
    upper="$(tr '[:lower:]' '[:upper:]' <<< "$run")"
    for template in catalog:lab_base catalog-ro:lab_base_ro; do
      sed -e "s/@RUN@/$run/g" -e "s/@RUN_UPPER@/$upper/g" "$LAB/trino/base/${template%%:*}.properties" \
        > "$RUN/trino/catalog/${template#*:}_$run.properties"
    done
    rules="${rules//@RUN@/$run}"
  fi
  printf '%s\n' "$rules" > "$RUN/trino/rules.json"
  chmod 0644 "$RUN/trino/rules.json" "$RUN/trino/catalog/"*.properties
}

# A baseline run's own Polaris warehouse and principal, then its Trino catalog (Trino restarts to load it).
base() {
  local run="$1"
  [[ "$run" =~ ^[a-z][a-z0-9_]{0,31}$ ]] || die "a run id is [a-z][a-z0-9_]{0,31}: '$run'"
  # lab_base_ro_<run> is run <run>'s read-only catalog and principal: run ro_x would collide with run x's.
  [[ "$run" != ro_* ]] || die "a run id must not start with ro_: '$run'"
  [[ -f "$RUN/.env" ]] || die "no lab stack in $RUN: lab/stack.sh up first"
  # base/loaded names the run Trino last restarted with, written only after that restart succeeded.
  if [[ "$(active_run)" == "$run" && "$(cat "$RUN/base/loaded" 2> /dev/null)" == "$run" \
    && -n "$(compose ps --status running -q trino)" ]]; then
    echo "baseline run $run is already active (catalog lab_base_$run)"
    return
  fi
  mkdir -p "$RUN/base"
  # The location ends in /warehouse, so no run's location is a prefix of another's (r1 and r10).
  jq -n --arg n "lab_base_$run" --arg ro "lab_base_ro_$run" --arg l "s3://lab-base/$run/warehouse" '{
    catalogs: [{name: $n, location: $l, properties: {"polaris.config.drop-with-purge.enabled": "true"}}],
    principals: [
      {name: $n, catalog: $n, grants: [{on: {type: "catalog"}, privileges: ["CATALOG_MANAGE_CONTENT"]}]},
      {name: $ro, catalog: $n, grants: [{on: {type: "catalog"}, privileges: ["NAMESPACE_LIST",
        "NAMESPACE_READ_PROPERTIES", "TABLE_LIST", "VIEW_LIST", "TABLE_READ_PROPERTIES", "TABLE_READ_DATA"]}]}
    ]
  }' > "$RUN/base/$run.json"
  compose run --rm -T setup python -I /lab/polaris-setup.py "/run/lab/base/$run.json"
  echo "$run" > "$RUN/base/active"
  rm -f "$RUN/base/loaded"
  render "$(mode_of_run)"
  compose up -d --wait --force-recreate trino
  echo "$run" > "$RUN/base/loaded"
  echo "baseline run $run active: catalog lab_base_$run, identity baseline_writer, s3://lab-base/$run/warehouse"
}

up() {
  local mode="${1:-base}" specs=(/lab/polaris.json)
  [[ "$mode" == base || "$mode" == spike ]] || die "unknown mode: $mode"
  if [[ -f "$RUN/.env" ]]; then
    [[ "$(mode_of_run)" == "$mode" ]] || die "the stack in $RUN was created as $(mode_of_run); run lab/stack.sh reset first"
    check_credentials
  else
    generate "$mode"
  fi
  render "$mode"
  if [[ "$mode" == spike ]]; then specs+=(/lab/spike/polaris.json); fi

  compose up -d --wait catalog-db seaweedfs polaris otel
  compose run --rm -T setup python -I /lab/polaris-setup.py "${specs[@]}"
  compose up -d --wait trino
  local users="$USERS"
  if [[ "$mode" == spike ]]; then users="$users $SPIKE_USERS"; fi
  echo "lab up ($mode): https://127.0.0.1:28443, users $users (passwords in $RUN/passwords);" \
    "OTLP on 127.0.0.1:24317 (gRPC) and 24318 (HTTP), written to $RUN/otel"
}

# A container on the lab network with one zone's REST catalog and S3 credentials in its environment: read from the
# run directory into this process's environment and passed by name (compose run -e NAME), so no value is ever on a
# command line or printed. lab.iceberg.rest_catalog() builds the PyIceberg catalog from them.
py() {
  local zone="${1:-}" principal prefix
  shift || true
  [[ "${1:-}" == -- ]] && shift
  case "$zone" in
    exp) LAB_REST_WAREHOUSE=lab_exp principal=POLARIS_LAB_EXP_CREDENTIAL prefix=S3_EXP ;;
    *) die "py: unknown zone '$zone' (exp)" ;;
  esac
  [[ -f "$RUN/polaris.env" ]] || die "no lab stack in $RUN: lab/stack.sh up first"
  LAB_ZONE="$zone"
  LAB_REST_CREDENTIAL="$(sed -n "s/^$principal=//p" "$RUN/polaris.env")"
  LAB_S3_ACCESS_KEY="$(sed -n "s/^${prefix}_ACCESS_KEY=//p" "$RUN/trino.env")"
  LAB_S3_SECRET_KEY="$(sed -n "s/^${prefix}_SECRET_KEY=//p" "$RUN/trino.env")"
  [[ -n "$LAB_REST_CREDENTIAL" && -n "$LAB_S3_ACCESS_KEY" && -n "$LAB_S3_SECRET_KEY" ]] \
    || die "py: zone $zone's credentials are missing in $RUN"
  LAB_RESULTS_DIR="${LAB_RESULTS_DIR:-$REPO/lab/results}"
  mkdir -p "$LAB_RESULTS_DIR"
  # The container has no git metadata (only lab/ and lakeops/ are mounted): results record the host's state.
  LAB_GIT_SHA="$(git -C "$REPO" rev-parse HEAD)"
  LAB_GIT_DIRTY="$(git -C "$REPO" status --porcelain -- . ':(exclude)lab/results' ':(exclude)lab/freezes' | head -1)"
  LAB_GIT_DIRTY="${LAB_GIT_DIRTY:+true}"
  export LAB_ZONE LAB_REST_WAREHOUSE LAB_REST_CREDENTIAL LAB_S3_ACCESS_KEY LAB_S3_SECRET_KEY LAB_RESULTS_DIR \
    LAB_GIT_SHA LAB_GIT_DIRTY
  compose run --rm -T -e LAB_ZONE -e LAB_REST_WAREHOUSE -e LAB_REST_CREDENTIAL -e LAB_S3_ACCESS_KEY \
    -e LAB_S3_SECRET_KEY -e LAB_GIT_SHA -e LAB_GIT_DIRTY py uv run --project /repo/lab "$@"
}

case "${1:-}" in
  up) up "${2:-base}" ;;
  down) if [[ -f "$RUN/.env" ]]; then compose --profile '*' down --remove-orphans; fi ;;
  reset)
    if [[ -f "$RUN/.env" ]]; then compose --profile '*' down -v --remove-orphans; fi
    # Deletes only a run directory this script made, or one under out/, judged by the resolved path (symlinks
    # followed): a mistyped LAB_RUN_DIR stays untouched.
    if [[ -e "$RUN" ]]; then
      real="$(cd "$RUN" && pwd -P)"
      case "$real" in / | "$HOME" | "$REPO" | "$REPO/out") die "refusing to delete $RUN: it is $real" ;; esac
      [[ "$real" == "$REPO/out/"?* ]] || grep -q '^LAB_MODE=' "$real/.env" 2> /dev/null \
        || die "refusing to delete $RUN ($real): not under $REPO/out and not a lab run directory"
      rm -rf "$real"
    fi
    ;;
  mem)
    ids="$(compose ps -q)"
    # Without ids docker stats would list every container on the machine, other stacks' too.
    [[ -n "$ids" ]] || die "no lab container is running"
    # shellcheck disable=SC2086 # one argument per container id
    docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' $ids
    ;;
  mem-peak)
    awk -F'\t' '{
      split($3, m, " / "); n = m[1] + 0
      if (m[1] ~ /GiB/) n *= 1024; else if (m[1] ~ /KiB/) n /= 1024
      if (n > peak[$2]) peak[$2] = n
      total[$1] += n
    } END {
      for (c in peak) printf "%s\t%.0f\n", c, peak[c]
      for (t in total) if (total[t] > max) max = total[t]
      printf "stack total (peak of the per-sample sum)\t%.0f\n", max
    }' "$2" | sort
    ;;
  base) base "${2:-}" ;;
  py) shift && py "$@" ;;
  base-drop)
    [[ -n "$(active_run)" ]] || die "no active baseline run"
    rm -f "$RUN/base/active" "$RUN/base/loaded"
    render "$(mode_of_run)"
    compose up -d --wait --force-recreate trino
    ;;
  compose) shift && compose "$@" ;;
  *) die "usage: lab/stack.sh up [spike] | down | reset | base RUN | base-drop | py ZONE -- ARGS | mem | mem-peak FILE"\
       "| compose ARGS" ;;
esac
