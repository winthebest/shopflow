# 0419. The lab is its own compose stack, on the platform's images and protections, and the harness refuses `lake`

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-data

## Context

The research experiments (compaction, retention, concurrent writers, maintenance, the guarded agent) need tables
they may damage. They set floors to 0s, run every table procedure, DROP, reset to a fresh state many times a day, and
let a baseline write freely. The platform's lakehouse cannot host that. Its `lake` catalog holds the CDC tables, ADR
0417 showed that any writer in a catalog may run every procedure there, and k3d time is an exclusive slot shared by
every lane. The numbers must still describe the platform: the same Trino, Polaris and SeaweedFS builds, behind the
same TLS, password and file-based rules (ADR 0410, 0411, 0417), on Iceberg v2 (ADR 0407).

## Decision

The lab is a separate docker compose stack (`lab/compose.yml`, project `shopflow-lab`), started with `lab/stack.sh`
or `make lab-up` and outside k3d.

1. **Images.** Trino, Polaris (server and admin tool), SeaweedFS and the OTel collector run at the platform's
   digests. `lab/tests/test_stack_pins.py` fails when a digest or tag differs from `deploy/platform/<component>`,
   and when a lab image is pinned without a digest. Only the lab has Postgres (Polaris's metastore; CNPG on the
   platform), the setup helper and the spike's OPA.
2. **Separate state.** The lab has its own volumes, its own Polaris realm and metastore, and its own buckets
   (`lab-exp`, `lab-evalprod`, `lab-sandbox`). It has no network path to k3d. Its warehouses, zones and identities
   are ADR 0417's.
3. **The platform's protections, not its secrets.** HTTPS (a self-signed certificate per stack), a bcrypt password
   file and file-based rules. `lab/stack.sh` generates every credential at the first `up` into `out/lab` (mode
   0700, gitignored), so nothing lab-related is in git or SOPS.
4. **Published on 127.0.0.1 only** (docs/contracts/environment.md): Trino HTTPS on 28443, OTLP gRPC and HTTP on
   24317 and 24318.
5. **The harness refuses `lake`** (`lab/src/lab/guard.py`). Before it hands out a session (`guarded_connect`), and
   for every table it touches (`check_table`):
   - every Iceberg catalog file in the run directory uses this stack's Polaris (`http://polaris:8181`) and
     SeaweedFS (`http://seaweedfs:8333`), and none is named `lake*` or uses warehouse `lake`;
   - every Polaris spec has lab warehouses only, located under `s3://lab-*/`;
   - an identity that sees a `lake*` catalog is on the platform's Trino;
   - targets are `lab_exp`, `lab_ops`, `lab_ro`, `lab_sbx` and `lab_base_<run>` only, and every table is stored
     under `s3://lab-*/` (its `SHOW CREATE TABLE` location).
6. **Results only from the lab.** Dev, baseline, held-out and eval runs need Trino's node environment `lab`
   (`/v1/info`; `lab/trino/etc/node.properties`). The platform's Trino runs the chart's default, `production`.
   `LAB_TRINO_URL` may point the harness at the k3d Trino for exploring (`require_lab=False`), never for numbers.
7. **Telemetry stays local.** An OTel collector receives OTLP from host processes (the eval harness, the agent's
   GenAI traces) and writes `out/lab/otel/{traces,metrics,logs}.jsonl`; it forwards nothing. Viewing traces in
   Tempo or Grafana means k3d's observability stack, on purpose: the lab must not depend on a slot.
8. **Size.** Memory limits add up to 4.9 GiB: Trino 3 GiB and 2 CPUs (as the platform's coordinator), SeaweedFS
   768 MiB, Polaris 768 MiB, Postgres 256 MiB, collector 128 MiB. One lab stack runs at a time on the machine.
   sf-data owns Docker time and books other lanes in, and the lab is down during a k3d slot.

Measured on a fresh stack on 2026-10-18: `make lab-test` (deny matrix plus harness tests) passed 196 tests in 67 s;
the stack's peak memory was 2128 MiB (Trino 1169, SeaweedFS 448, Polaris 401, Postgres 80, collector 30).

## Alternatives considered

| Option | Why not |
|---|---|
| Lab namespaces in the platform's `lake` warehouse | Floors at 0s and free procedures next to the CDC tables; any writer in `lake` may run every procedure there (ADR 0417) |
| Lab catalogs on the k3d Trino, with their own warehouses | Shares Polaris and SeaweedFS with `lake`; needs an exclusive slot for every experiment; each reset goes through GitOps |
| Another engine or object store (DuckDB, MinIO) | Results would not carry over to the platform's Trino, Polaris and SeaweedFS |
| Guard by URL (`127.0.0.1:28443`) | A port-forward can put any Trino behind that address. The node environment and the visible catalogs belong to the server |

## Consequences

- Positive: experiments reset in minutes without a slot, on the engine and catalog the platform runs; the platform's
  data is out of reach by construction (separate stack) and by check (the guard).
- Negative / risks: every image bump is made twice; the pin test makes a missed one fail CI. The lab's certificate,
  passwords and keys change at every reset; tests and probes read them from `out/lab`. No trace UI in the lab.
- When to revisit: the platform changes engine, catalog or object store; experiments need more than about 5 GiB;
  results have to come from the cloud.

Reproduce: `make lab-up && make lab-test` (`make lab-reset` first for a fresh stack); `make lab-check` needs no
stack.
