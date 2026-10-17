# 0417. Lab safety boundary: one Trino catalog per zone, read-only in the engine, own warehouse, principal and key

- Status: Accepted
- Date: 2026-10-16
- Lane: sf-data

## Context

The lab (Phases 9–14) puts research harness, AI planner and executor on one Trino. File-based access control cannot
restrict table procedures (ADR 0410), so before anything is built on the lab we measured which layer refuses which
operation. Three questions: does `iceberg.security=read_only` refuse `ALTER TABLE … EXECUTE`; do Polaris grants per
catalog credential (per namespace) refuse it; does Trino's OPA plugin see table procedures.

Method (`lab/spike/`): the lab stack (`lab/compose.yml`) plus an overlay with OPA and nine probe catalogs over one
Polaris warehouse, each turning on exactly one restriction. `boundary-probe.py` runs 13 operations through each
catalog as identity `spike`, every time on a fresh table (4 snapshots, 3 data files, one planted orphan file), and
reads the state back through the unrestricted catalog. 130 cases on 2026-10-16 14:41Z, Trino 483
(`sha256:db58cc93…`, the platform's digest), Polaris 1.8.0, SeaweedFS 4.48, OPA 1.21.1. "yes" = ran and took
effect, "no" = refused:

| Operation | none (control) | Trino `READ_ONLY` | file rule `read-only` | file rule, no table privileges | Polaris reader | Polaris ns grant: write | Polaris ns grant: read | S3 key Read+List | OPA | catalog `allow: none` |
|---|---|---|---|---|---|---|---|---|---|---|
| SELECT | yes | yes | yes | no | yes | yes | yes | yes | yes | no |
| INSERT, DELETE | yes | no | no | no | no | yes | no | no | yes | no |
| SET PROPERTIES | yes | no | no | no | no | yes | no | yes | yes | no |
| CREATE TABLE | yes | no | no | no | no | yes | no | no | yes | no |
| DROP TABLE | yes | no | no | no | no | yes | no | yes | yes | no |
| `optimize` | yes | no | yes | no | no | yes | no | no | no | no |
| `expire_snapshots` | yes | no | yes | no | no | yes | no | yes | no | no |
| `remove_orphan_files` | yes | no | yes | no | yes | yes | yes | no | no | no |
| `rollback_to_snapshot` | yes | no | yes | no | no | yes | no | yes | no | no |
| `CALL system.unregister_table` | no | no | no | no | no | no | no | no | no | no |
| `SET SESSION *_min_retention` | no | no | no | no | no | no | no | no | no | no |

1. **`read_only` refuses table procedures: yes.** All four: `PERMISSION_DENIED: Access Denied: Cannot execute table
   procedure EXPIRE_SNAPSHOTS on probe.<table>` (same for OPTIMIZE, REMOVE_ORPHAN_FILES, ROLLBACK_TO_SNAPSHOT). It is
   the only Trino layer that does so in a catalog the identity can read: with the file rule `allow: read-only` all
   four ran (Trino's docs say so too). Without table privileges they fail only because they need SELECT (`Cannot
   select from table`), so any reader could run them.
2. **Polaris grants per credential: yes, per namespace, for everything that commits through the catalog.** The
   reader principal and the read-only namespace were refused by name, e.g. `Authorization denied for principal
   'spike_ns' on operation 'REMOVE_TABLE_SNAPSHOTS': missing TABLE_REMOVE_SNAPSHOTS on TABLE_LIKE …` (rollback:
   `SET_TABLE_SNAPSHOT_REF`; optimize, INSERT, DELETE: `ADD_TABLE_SNAPSHOT`; DROP: `DROP_TABLE_WITH_PURGE`; CREATE:
   `TABLE_CREATE` on the namespace). **Not** `remove_orphan_files`: it lists and deletes with the engine's S3 key and
   never asks the catalog. Grants name each namespace; there is no prefix wildcard.
3. **OPA sees table procedures: yes.** 40 decisions for 40 procedure calls, with input `{"operation":
   "ExecuteTableProcedure", "resource": {"table": {"catalogName", "schemaName", "tableName"}, "function":
   {"functionName": "EXPIRE_SNAPSHOTS"}}}`; a deny gives the same `Cannot execute table procedure …` message.

Also found:

- `allow: none` refuses every operation on an existing table, procedures included (`Access Denied: Cannot access
  catalog spike_hidden`). For a name that does not exist Trino answers `Table '…' does not exist` / `Schema '…' does
  not exist` instead: an identity can test whether a name exists in a catalog it cannot see. Listings are refused.
- A read-only S3 key stops data writes and `remove_orphan_files`, not history: `expire_snapshots` (4 snapshots → 1)
  and `rollback_to_snapshot` commit through Polaris, which writes the metadata with its own key, and DROP ran because
  Polaris purges with its own key.
- Polaris refuses Trino's DROP TABLE, which is a purge, unless the catalog sets `polaris.config.drop-with-purge.enabled`
  (`Unable to purge entity …`; off by default, so also on the platform's `lake`).
- `CALL` procedures are refused for everyone: file-based access control has no `procedures` rule.
- Without a `queries` section, file-based access control lets every user view and kill every query (found in review,
  then reproduced on 2026-10-16): `agent_planner` saw `executor`'s running query in `system.runtime.queries` (with its
  SQL), `GET /v1/query/<id>` answered 200, and `DELETE /v1/query/<id>` answered 204 and the query FAILED.
- The other defaults of the file-based rules, checked as `agent_planner` the same day: impersonating `executor`, table
  functions (`table_changes`), `CALL system.runtime.kill_query` and `ALTER SCHEMA … SET AUTHORIZATION` are refused;
  system session properties (`SET SESSION query_max_memory = …`) and `system.runtime.nodes` are open to everyone.
  Those set resources and show the node, not data; they stay open here.

## Decision

The boundary is the Trino catalog, and each catalog belongs to one zone:

1. Each identity gets its catalogs from the file-based rules (`lab/trino/rules.json`); every other catalog is
   `allow: none`.
2. Catalogs for readers set `iceberg.security=READ_ONLY`: the engine refuses writes and table procedures.
3. Each zone has its own Polaris catalog (warehouse) and bucket; each Trino catalog has its own Polaris principal,
   granted on its zone's warehouse only (readers: read privileges), and its own S3 key (readers: Read + List). If the
   engine layer were lost, Polaris would still refuse expire, rollback, optimize and writes, and the key would still
   refuse `remove_orphan_files`.
4. Nobody may set `*_min_retention` by session property. Floors: `lab_ops`, `lab_sbx`, `lab_ro` 7d; `lab_exp` 0s.
5. `drop-with-purge` only on the warehouses where dropping is normal work (`lab_exp`, `lab_sandbox`).
6. Queries: `"queries": [{"allow": ["execute"]}]`. Everyone may run queries; each identity sees and cancels only its
   own (owners always may), so no identity reads another's SQL or kills its work.

| Catalog | Identity | Warehouse, bucket | Mode |
|---|---|---|---|
| `lab_exp` | `harness` | `lab_exp`, `lab-exp` | read/write, floors 0s, drop allowed |
| `lab_ops` | `executor` | `lab_evalprod`, `lab-evalprod` | read/write, floors 7d, no drop |
| `lab_ro` | `agent_planner` | `lab_evalprod`, `lab-evalprod` | read only in Trino, Polaris and S3 |
| `lab_sbx` | `executor` | `lab_sandbox`, `lab-sandbox` | read/write, floors 7d, drop allowed |

`lakeops/tests/test_deny_matrix.py` checks it: 3 identities × 4 catalogs × 15 operations, the catalog listing, and
query isolation (for every pair of identities, the viewer finds no row in `system.runtime.queries` for the owner's
running query and gets 403 from `GET` and `DELETE /v1/query/<id>`, while the owner gets 200 and 204; each identity
sees only its own queries). 190 tests passed on a fresh stack, 2026-10-16 15:07Z, 66 s; the 9 query tests failed
before the `queries` rule. The `lab_base_<run>` catalogs come with the harness.

## Alternatives considered

| Option | Why not |
|---|---|
| OPA in front of every query | Works (identity × table × procedure), but every query then depends on one more service, and a Rego policy duplicates `rules.json`. The catalog split already separates the zones. Kept for the case below |
| Polaris namespace grants in one warehouse | Each eval task and sandbox gets new namespaces, and each would need an admin grant (no wildcards); does not stop `remove_orphan_files` |
| File-based rules only, one writable catalog | Every reader can run every table procedure |
| Read-only S3 keys as the boundary | `expire_snapshots`, `rollback_to_snapshot` and DROP still pass |

## Consequences

- Positive: three independent layers for `agent_planner`, each measured; the matrix fails if a rule, a catalog
  property or a Trino upgrade changes any of the 180 catalog outcomes or the query isolation.
- Negative / risks: four catalogs with near-identical properties and one credential pair per zone. Schema and table
  names leak across catalogs through error messages; contents and listings do not.
- Platform: the findings confirm ADR 0410's three layers for `lake_ro` (the RO key covers `remove_orphan_files`), and
  `lake` cannot DROP through Trino while drop-with-purge is off. Nothing on the platform changes here.
- When to revisit: one writable catalog needs different procedures per table or identity (then OPA, measured above);
  Trino's file-based rules gain procedure privileges; Polaris adds prefix grants.

Reproduce: `lab/spike/run.sh` (fresh stack, about 3 minutes; evidence in `out/lab-spike/`), and
`lab/stack.sh up && (cd lakeops && uv run pytest -m lab)`.

## Update 2026-10-18: baseline runs, compressed floors

- **Baseline runs.** A baseline arm (the agent with broad SQL) works in a catalog of its own run, `lab_base_<run>`, for
  identity `baseline_writer` only (`lab/stack.sh base <run>`, one active run at a time).
  - The catalog is the run's own Polaris warehouse on `s3://lab-base/<run>/warehouse`, with drop-with-purge, and its own
    principal.
  - The S3 key covers the `lab-base` bucket, so between runs the barrier is Polaris's location check. Polaris refuses a
    table outside the catalog's warehouse with 403 "… is not in the list of allowed locations"; Trino reports it as
    "Failed to create transaction". Checked on the REST API on 2026-10-18; the deny matrix's `create_table_elsewhere`
    covers it for every writer.
- **Floors** are 7 simulated days, `7m` at the published time scale (ADR 0420), on `lab_ops`, `lab_sbx`, `lab_ro` and
  `lab_base_<run>`.
