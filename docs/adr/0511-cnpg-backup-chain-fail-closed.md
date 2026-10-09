# 0511. Postgres backup chain with a pointer, failing closed

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

Every session recreates the Postgres cluster. The dangerous failure is silent: a lost pointer or a wrong bootstrap
mode makes CloudNativePG run `initdb`, the shop starts with an empty database, and new WAL may overwrite the archive
of the real history.

## Decision

- SSM holds `pg-backup-pointer = {serverName, backupId}` (last completed backup) and a write-once `pg-initialized`
  marker.
- `cloud-up`: pointer present and its `backup.info` exists in S3 → `bootstrap.recovery` from that `serverName`
  (latest or `--pitr`) into a **new** `serverName` (`shop-db-<session>`); no pointer and no marker → `initdb`, then
  write the marker; **marker present but pointer or backup missing → stop**, never `initdb`.
- After recovery: on-demand `Backup` (barman-cloud plugin) → wait `completed` → move the pointer → only then smoke.
- `cloud-down`: `CHECKPOINT; SELECT pg_switch_wal()`, wait until that segment is archived, final `Backup`, move the
  pointer. CNPG `retentionPolicy: 30d` owns expiry; `pg-backup/` has no S3 lifecycle rule.

## Alternatives considered

| Option | Why not |
|---|---|
| Always `initdb` + reseed | loses every order between sessions; no restore test |
| Recover from "latest" without a pointer | cannot detect a missing or wrong chain; reusing a `serverName` mixes WAL histories |
| S3 lifecycle on backups | could delete a base backup the pointer still references |

## Consequences

- Positive: every session start is a tested restore; a broken chain stops loudly instead of losing data.
- Negative: a manual step when the chain breaks (runbook: rewrite the pointer, or delete the marker after a written
  decision); the reaper paths have no final backup (RPO = WAL `archive_timeout`, 5 min by default).
- When to revisit: if CNPG changes its backup/recovery API (plugin vs in-tree).
