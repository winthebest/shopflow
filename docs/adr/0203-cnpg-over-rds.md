# 0203. Postgres with CloudNativePG on both k3d and EKS (not RDS)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-platform

## Context

The shop writes orders to Postgres, and Phase 4 reads them through logical replication (Debezium). The same
manifests must run on a laptop and on an EKS cluster that is created and destroyed per session, within a $100 AWS
budget. The project also has to prove backup, restore and failure drills (Phase 7) with numbers, which needs
control over the database lifecycle.

## Decision

- CloudNativePG operator 1.30.1 (chart 0.29.1, image pinned by digest) at wave -1.
- `Cluster shop-db` in namespace `shop` in its own app at wave 0: 1 instance locally, PostgreSQL 17.11
  (`minimal-trixie` image pinned by digest), `wal_level=logical`, `bootstrap.initdb` database `shop` with owner
  `shop_app` (non-superuser; superuser access disabled). CNPG creates Secret `shop-db-app` (`uri`, `password`, …),
  which the orders service and the migration Job read, so no database password lives in Git.
- Other consumers get declarative login roles (`managed.roles`): `debezium` (REPLICATION), `trino_pg`, `polaris`
  (owner of the CNPG `Database` `catalog`; `pg_hba` lets it reach only that database). Their passwords are SOPS
  Secrets locally and come from SSM via External Secrets on AWS; table grants stay in Alembic.
- The database is a separate app from the shop chart: the shop's PreSync migration hook runs before any resource
  of its own app, so the Cluster must already exist and be healthy (earlier wave).
- AWS (Phase 6): same Cluster, with backups to S3 through the barman-cloud plugin and a per-session `serverName`.
- Local restore drill (Phase 7, profile `drill` or `data`): the same plugin backs up to SeaweedFS in `lakehouse`
  (bucket `pg-backup`, endpoint + the keys of sf-data's identity `cnpg-backup`, copied into `shop` by the plugin
  app), so a drill can destroy the database and restore it, PITR included, without AWS. The chain name and the
  restore point are the same session params as on AWS (`pg.serverName`, `pg.recoveryFrom`, `pg.recoveryTargetTime`).
  Locally, archiving runs only while `pg.serverName` is set; `make up` sets it when the profiles deploy the plugin.
  A cluster without SeaweedFS would otherwise keep WAL until the 5Gi volume is full.
- `archive_timeout: 60s` everywhere: a WAL segment is archived at least once a minute even when it is not full, so
  the RPO is about a minute (CNPG's default is 5 minutes).

## Alternatives considered

| Option | Why not |
|---|---|
| Amazon RDS / Aurora | Costs credit around the clock or needs snapshot/restore per session; different config locally vs AWS; logical replication and failure drills are less under our control |
| Bitnami PostgreSQL chart | Bitnami images moved to a legacy/paid model in 2025 |
| Plain StatefulSet + custom scripts | Re-implements failover, backups and role management that CNPG already provides |
| Crunchy PGO / Zalando operator | Viable, but CNPG has the most active community, a Kubernetes-native backup plugin, and declarative roles |

## Consequences

- Positive: one database definition for both environments; backups and PITR are declarative; logical replication
  is on from day 1; credentials are generated in-cluster.
- Negative / risks: the team operates Postgres itself (upgrades, storage, backups). Locally a single instance on
  local-path storage means no HA and data is lost with `make down` (acceptable: `seed` restores demo data).
  `wal_level=logical` with an abandoned replication slot would fill the disk, so `max_slot_wal_keep_size` is 2GB
  (5Gi volume): past it the slot is invalidated and CDC re-snapshots into a new epoch, which is better than a
  stopped shop. Phase 4 alerts on slot lag well before the cap.
- When to revisit: the project needs managed HA across AZs for real users, or CNPG stops tracking new Postgres
  major versions.
