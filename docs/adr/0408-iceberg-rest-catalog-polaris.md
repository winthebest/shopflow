# 0408. Iceberg REST catalog: Apache Polaris (not Lakekeeper)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-data

## Context

Locally (and in the Phase 9 lab) the lake needs an Iceberg REST catalog with metadata in Postgres (CNPG). Phase 9
needs more than "a catalog": the safety boundary is per catalog and identity (Trino file-based access control
cannot restrict table procedures), so the catalog server should enforce per-credential permissions as a second
layer — a read-only Trino catalog must also hold catalog credentials that cannot commit. Spike on 2026-10-09
(Polaris 1.8.0 of 2026-09-28; Lakekeeper 0.14.0 of 2026-10-07):

| Need | Polaris 1.8 | Lakekeeper 0.14 |
|---|---|---|
| Machine credentials for Trino/sink | Built-in OAuth2 client-credentials token endpoint, one principal per client | Does not issue credentials; needs an external IdP (Keycloak, Entra) |
| Authorization in OSS | Built-in RBAC: principal → principal role → catalog role → privileges (catalog/namespace/table, e.g. `TABLE_WRITE_DATA`) | OpenFGA (separate server + its own DB); Cedar policies only in the commercial edition |
| Postgres metastore | `relational-jdbc` | yes |
| S3-compatible storage without STS | `stsUnavailable`, `pathStyleAccess`, `endpoint` per catalog | yes |
| Kubernetes | Official Helm chart (Apache release repo) + admin tool image | Helm chart |
| Footprint | JVM (Quarkus); ran within a 640Mi limit in the smoke test | Rust, small — but + Keycloak + OpenFGA for the same features |

## Decision

Apache Polaris 1.8.0 (Apache top-level project since 2026-02) with the official chart, `relational-jdbc` on CNPG
(database `catalog`), one catalog `lake` (`s3://lake/warehouse`, `stsUnavailable: true`). A setup Job
(`polaris-setup.py`) creates catalog, namespace `bronze`, and one principal per client with least privilege:
`iceberg_sink` (read/write data in namespace `bronze`), `trino_lake` (`CATALOG_MANAGE_CONTENT`), `trino_lake_ro`
(read only). It writes each principal's generated credentials into a Secret next to its consumer.

## Alternatives considered

| Option | Why not |
|---|---|
| Lakekeeper | Per-credential RBAC needs Keycloak + OpenFGA (two more stateful services) |
| Nessie | Git-like catalog branching is not needed; future of the project less clear than Polaris |
| Iceberg REST fixture (`apache/iceberg-rest-fixture`) | Test fixture, no auth |
| Hive Metastore / JDBC catalog | No REST API for Trino/DuckDB/PyIceberg alike; no per-client permissions |

## Consequences

- Positive: defense in depth for Phase 9: verified in the smoke test, the read-only principal reads bronze but
  `create_namespace` returns 403; the sink principal can write only `bronze`.
- Found by the smoke test: with `stsUnavailable` the server writes metadata through the AWS SDK default
  credential chain (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`), not the chart's `storage.secret`; PyIceberg must
  not request vended credentials (`X-Iceberg-Access-Delegation` header emptied).
- Negative / risks: object-store permissions are not vended, so read-only S3 keys are a separate control
  (SeaweedFS identity `trino-lake-ro`); token signing keys are per pod (one replica locally).
- When to revisit: Lakekeeper ships built-in client credentials + OSS authorization, or Polaris RBAC proves
  insufficient for the Phase 9 refusal matrix.
