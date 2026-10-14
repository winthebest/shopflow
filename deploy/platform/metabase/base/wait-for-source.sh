#!/bin/sh
# Runs copy-secret.py until its source Secret exists. On a cluster's first sync this Job can start before the other
# apps have created shop/shop-db-metabase or lakehouse/trino-metabase: a missing Secret (HTTP 404) is retried every
# 5 s for up to 5 minutes in the same pod, instead of a failed pod per attempt. Any other error fails at once, and the
# Job's backoffLimit retries it.
for _ in $(seq 1 60); do
  if out="$(python -I /scripts/copy-secret.py 2>&1)"; then
    echo "$out"
    exit 0
  fi
  echo "$out"
  case "$out" in
    *"HTTP 404"*) sleep 5 ;;
    *) exit 1 ;;
  esac
done
exit 1
