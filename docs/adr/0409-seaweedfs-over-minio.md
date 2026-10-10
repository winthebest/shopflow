# 0409. Local object storage: SeaweedFS (not MinIO)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Locally the lake (and CNPG backups, Phase 4 step 9) needs an S3-compatible store that Iceberg's `S3FileIO`,
Trino's native S3 file system, PyIceberg and Polaris all accept, in little memory. On AWS the lake is S3. MinIO
Community Edition stopped publishing images (Oct 2025) and its repository was archived (2026-04).

## Decision

SeaweedFS 4.48 as a single `weed mini` process (master + volume + filer + S3 gateway): one Deployment, one PVC,
only the S3 port (8333) exposed, WebDAV/admin UI/telemetry/built-in Iceberg catalog disabled, bucket `lake` created
at start. Runs as uid 1000 with a read-only root filesystem. S3 identities are scoped to the bucket, one per
client; the read-only Trino catalog gets `Read`/`List` only, so it cannot delete files even through procedures
that bypass the catalog (`remove_orphan_files`).

A second bucket, `pg-backup`, holds shop-db's WAL archive and base backups (sf-platform's CNPG barman plugin, profile
`data` and the restore drill). Its identity `cnpg-backup` (`Read`/`Write`/`List` on `pg-backup` only) lives in its own
Secret, not in the lake's `s3.json`. The lake identities are one SOPS group whose secret keys only exist encrypted, so
adding an identity there would regenerate every lake credential. An init container instead merges the backup identity
into `s3.json` when the pod starts (`deploy/platform/seaweedfs/base/merge-s3-config.py`), and SeaweedFS reads the
merged file from a memory-backed volume. A changed credential takes effect on the next pod start
(`kubectl -n lakehouse rollout restart deploy/seaweedfs`).

## Alternatives considered

| Option | Why not |
|---|---|
| MinIO CE | Archived; no maintained images |
| Garage | Small and stable, but S3 coverage needs per-feature checks for Iceberg writers |
| Ceph RGW (Rook) | Several GB of memory |
| LocalStack | Emulator, licensing changes |

## Consequences

- Positive: verified end to end in the smoke test (Polaris metadata writes, sink Parquet writes, PyIceberg reads
  with the read-only identity) within a 384Mi memory limit.
- Negative / risks: single process, single replica; data is lost with the cluster (`make down`), which the epoch
  design tolerates.
- When to revisit: S3 API gaps (multipart, conditional writes) show up as Iceberg write errors; then try Garage.
