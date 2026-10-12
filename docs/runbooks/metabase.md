# Runbook: Metabase (profile `bi`)

| | |
|---|---|
| Signal | Job `metabase/metabase-provision` failed (app `metabase` Degraded after a sync), or the dashboard "Shopflow sales" is missing or empty |
| Code | [`deploy/platform/metabase`](../../deploy/platform/metabase) (`provision.py`, `dashboard/`), check [`scripts/data-metabase-check.sh`](../../scripts/data-metabase-check.sh) |
| Decision | [docs/adr/0416](../adr/0416-metabase-bi-dashboards-from-git.md) |

## What it means

Metabase's admin, its Trino connection (`lake_ro`, user `metabase`, schema `gold`) and the dashboard are not kept in
Metabase: the PostSync Job `metabase-provision` creates or updates them from git on every sync. A failed Job leaves
Metabase as it was (on a new cluster: no admin, so the setup wizard is open to whoever reaches the port-forward).

## Triage

`K="kubectl --context k3d-<cluster>"`; the Job's log names the failing call:
`$K -n metabase logs job/metabase-provision`.

- `Metabase did not become healthy in time`: the Deployment did not start. `$K -n metabase describe pod -l
  app.kubernetes.io/name=metabase`: Secret `metabase-db` missing (the copy Job `metabase-secret-copy` failed, or
  shop-db has no role `metabase`), or the application database unreachable (NetworkPolicy `metabase-flows`).
- `POST /api/session: HTTP 401`: the admin password in Secret `metabase-admin` no longer matches the one Metabase
  stored at its first start (the Secret was regenerated after the application database was created). Metabase keeps
  the first admin's password: set it back, or start from an empty database `metabase`.
- `POST /api/database` or `PUT /api/database/<id>`, HTTP 400: Metabase tested the Trino connection and it failed.
  Common causes: Trino not ready, or the login wrong (Secret `trino-metabase` copied from `lakehouse/trino-metabase`,
  bcrypt in `trino-password-db`, both from `scripts/data-secrets.sh`). A TLS error means the CA in Secret
  `metabase-trino-ca` (cert-manager) is missing.
- Questions on the dashboard show an error: the SQL in `dashboard/*.sql` or a gold mart changed. An empty question
  usually means gold has no data yet: profile `batch` builds it hourly.

## Mitigation

Fix the cause, then run the hooks again ([data-setup-hooks.md](data-setup-hooks.md), step 4). The Job is idempotent.
Verify with `CLUSTER=<cluster> scripts/data-metabase-check.sh`: gold readable, silver, catalog `lake` and writes
denied, every question returning rows.
