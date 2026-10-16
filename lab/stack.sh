#!/usr/bin/env bash
# Lakehouse lab stack (lab/compose.yml).
#   lab/stack.sh up [spike]   create or start the stack; `spike` adds the lab/spike overlay (OPA, layer-probe catalogs)
#   lab/stack.sh down         stop and remove the containers; volumes and credentials stay
#   lab/stack.sh reset        remove containers, volumes and $LAB_RUN_DIR (every credential)
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
#   lab_ops  executor        warehouse lab_evalprod  bucket lab-evalprod  read/write, floors 7d
#   lab_ro   agent_planner   warehouse lab_evalprod  bucket lab-evalprod  read only (Trino, Polaris and S3)
#   lab_sbx  executor        warehouse lab_sandbox   bucket lab-sandbox   read/write, floors 7d
# Nobody may set the *_min_retention session properties. Every other catalog is `allow: none` for every identity.
# Needs docker compose, openssl, htpasswd, jq.
set -euo pipefail

LAB="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(cd "$LAB/.." && pwd -P)"
RUN="${LAB_RUN_DIR:-$REPO/out/lab}"
USERS="harness agent_planner executor"
BUCKETS="lab-exp,lab-evalprod,lab-sandbox"
# S3 identities: name, env prefix in trino.env (empty: not a Trino key), actions.
S3_IDENTITIES="polaris||Read:lab-exp,List:lab-exp,Write:lab-exp,Read:lab-evalprod,List:lab-evalprod,Write:lab-evalprod,Read:lab-sandbox,List:lab-sandbox,Write:lab-sandbox
exp|EXP|Read:lab-exp,List:lab-exp,Write:lab-exp
evalprod|EVALPROD|Read:lab-evalprod,List:lab-evalprod,Write:lab-evalprod
evalprod-ro|EVALPROD_RO|Read:lab-evalprod,List:lab-evalprod
sandbox|SANDBOX|Read:lab-sandbox,List:lab-sandbox,Write:lab-sandbox"
SPIKE_USERS="spike"
SPIKE_BUCKETS="lab-spike"
SPIKE_S3_IDENTITIES="spike|SPIKE|Read:lab-spike,List:lab-spike,Write:lab-spike
spike-ro|SPIKE_RO|Read:lab-spike,List:lab-spike"

die() { echo "lab/stack.sh: $*" >&2; exit 1; }
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

# Trino's rules and catalogs: the base files, plus the overlay's (its rules go first: the first matching rule wins).
render() {
  local mode="$1"
  rm -f "$RUN/trino/catalog/"*.properties
  cp "$LAB/trino/etc/catalog/"*.properties "$RUN/trino/catalog/"
  if [[ "$mode" == spike ]]; then
    cp "$LAB/spike/trino/catalog/"*.properties "$RUN/trino/catalog/"
    jq -s '.[0] as $o | .[1] | with_entries(.value = (($o[.key] // []) + .value))' \
      "$LAB/spike/trino/rules.json" "$LAB/trino/rules.json" > "$RUN/trino/rules.json"
  else
    cp "$LAB/trino/rules.json" "$RUN/trino/rules.json"
  fi
  chmod 0644 "$RUN/trino/rules.json" "$RUN/trino/catalog/"*.properties
}

up() {
  local mode="${1:-base}" specs=(/lab/polaris.json)
  [[ "$mode" == base || "$mode" == spike ]] || die "unknown mode: $mode"
  if [[ -f "$RUN/.env" ]]; then
    [[ "$(mode_of_run)" == "$mode" ]] || die "the stack in $RUN was created as $(mode_of_run); run lab/stack.sh reset first"
  else
    generate "$mode"
  fi
  render "$mode"
  if [[ "$mode" == spike ]]; then specs+=(/lab/spike/polaris.json); fi

  compose up -d --wait catalog-db seaweedfs polaris
  compose run --rm -T setup python -I /lab/polaris-setup.py "${specs[@]}"
  compose up -d --wait trino
  local users="$USERS"
  if [[ "$mode" == spike ]]; then users="$users $SPIKE_USERS"; fi
  echo "lab up ($mode): https://127.0.0.1:28443, users $users (passwords in $RUN/passwords)"
}

case "${1:-}" in
  up) up "${2:-base}" ;;
  down) if [[ -f "$RUN/.env" ]]; then compose --profile '*' down --remove-orphans; fi ;;
  reset)
    if [[ -f "$RUN/.env" ]]; then compose --profile '*' down -v --remove-orphans; fi
    rm -rf "$RUN"
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
  compose) shift && compose "$@" ;;
  *) die "usage: lab/stack.sh up [spike] | down | reset | mem | mem-peak FILE | compose ARGS" ;;
esac
