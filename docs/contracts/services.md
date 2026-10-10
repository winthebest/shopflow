# Contract: shop services

Owner: orchestrator. Changes go through a PR reviewed by the orchestrator, because `sf-app` (code) and
`sf-platform` (Helm chart) both depend on this file.

## Services

| Service | Port | Purpose | Calls |
|---|---|---|---|
| `gateway` | 8000 | Public API: `GET /products`, `POST /checkout`, `GET /orders/{id}` | `orders` |
| `orders` | 8001 | Creates orders + order_items in one transaction, calls payments, sets status `pending → paid \| failed` | `payments`, Postgres |
| `payments` | 8002 | Mock payment provider with configurable latency/failure; `POST /charges` is idempotent per `order_id` (same `charge_id` and outcome on every call, decline decided by a hash of `order_id`), so orders may retry it | — |

Timeouts: gateway → orders 1s (no retry: `POST /checkout` is not idempotent), orders → payments 800ms in total: up to
3 attempts inside that deadline, full-jitter backoff, behind a circuit breaker per orders process (ADR 0102).

### `POST /checkout` responses (gateway)

A payment decline is a business outcome, not a server error: it must never be a 5xx (the availability SLO counts
every gateway 5xx as bad).

| Case | Status | Body |
|---|---|---|
| Paid | `201` | order, `status: "paid"` |
| Declined by payments | `201` | order, `status: "failed"` |
| Invalid input, unknown customer/product, total too large | `422` | `detail` |
| Payments timed out (order settled `failed`) | `504` | `detail`, `order_id`, `status` |
| Payments unreachable / bad answer (order settled `failed`) | `502` | `detail`, `order_id`, `status` |
| Payments circuit open (checked before the order is created: no order) | `503` + `Retry-After` | `detail` |
| Orders timed out / unreachable / 5xx (incl. DB errors) | `504` / `502` | `detail` |

## Common runtime contract

- **Health**: `GET /healthz` (liveness, no dependencies) and `GET /readyz` (readiness). Readiness checks only a
  service's own hard dependency: `orders` checks the DB (≤1s); `gateway` and `payments` have none. Gateway must
  stay Ready when orders is down so it answers 502/504 itself (and emits server spans) instead of Envoy's
  `503 no healthy upstream`.
- **Shutdown**: handle SIGTERM, stop accepting requests, drain in-flight requests within 20s.
- **Logs**: JSON lines on stdout, one object per line, with these keys (exact names):
  `timestamp` (RFC 3339, UTC), `level` (`DEBUG|INFO|WARNING|ERROR`), `message`, `service`, `trace_id`,
  `span_id` (32/16 lowercase hex, empty string when no active span). Extra keys are allowed.
- **Container**: non-root UID/GID `10001`; works with `readOnlyRootFilesystem: true` (only `/tmp` writable).
- **Telemetry**: OpenTelemetry SDK. Images default to `OTEL_SDK_DISABLED=true`; the local chart values turn it on
  from Phase 3 (wave 2) because every validation profile includes `obs` or `obs-lite` (a `core`-only cluster then
  only logs exporter warnings). When on:

  | Variable | Value |
  |---|---|
  | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-gateway.observability.svc:4317` (gRPC; HTTP is `:4318`) |
  | `OTEL_SERVICE_NAME` | `gateway` \| `orders` \| `payments` |
  | `OTEL_SEMCONV_STABILITY_OPT_IN` | `http` (spans carry `http.route` and `http.response.status_code`) |
  | `OTEL_TRACES_SAMPLER` | unset (default `parentbased_always_on`). SLIs are computed from server spans via the spanmetrics connector, so traces must not be sampled below 100%. |
  | `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=<local\|aws>` |

## Environment variables

| Service | Variable | Example / default |
|---|---|---|
| gateway | `ORDERS_URL` | `http://orders.shop.svc:8001` |
| orders | `DATABASE_URL` | from Secret `shop-db-app` key `uri` (CNPG creates it for the `bootstrap.initdb` owner `shop_app`) |
| orders | `PAYMENTS_URL` | `http://payments.shop.svc:8002` |
| orders | `PAYMENTS_ATTEMPTS` | `3` (attempts inside the 800ms deadline, ADR 0102) |
| orders | `PAYMENTS_ATTEMPT_TIMEOUT_MS` | `500` (per attempt, trimmed to the time left) |
| orders | `PAYMENTS_BREAKER_WINDOW_S` / `_MIN_CALLS` / `_FAILURE_RATIO` / `_OPEN_S` | `10` / `20` / `0.75` / `5` |
| payments | `PAYMENT_LATENCY_MS` | `50` |
| payments | `PAYMENT_FAILURE_RATE` | `0.02` (0–1; decided per `order_id` by hash, so a retry gets the same answer) |
| all | `LOG_LEVEL` | `INFO` |

## Database

- CNPG `Cluster` name `shop-db`, namespace `shop`, `bootstrap.initdb` database `shop`, owner `shop_app`
  (non-superuser; CNPG creates Secret `shop-db-app` for this owner).
- `updated_at` is the writing transaction's `now()`, not commit order: time-based cutoffs on it need a lookback window.
- Schema is owned by Alembic in `services/orders/migrations/` (only owner of the schema).
- Migration runs as a Kubernetes Job before the services roll out: image = orders image, command `["migrate"]` (wraps `alembic upgrade head`).
- A merged migration reaches clusters only through the shop chart's image pins (`deploy/charts/shop/values.yaml`). Once
  the migration commit's images are published, the next step is a PR bumping the three shop images to that `sha-<commit>`,
  before any cluster test relies on the schema. `platform-validate.sh` fails on `main` (and on PRs touching the chart)
  while the pinned orders image is older than the newest migration.
- `wal_level=logical` from day 1 (CDC in Phase 4).

### CDC source objects (Phase 4; consumers: sf-data)

| Object | Created by | Definition |
|---|---|---|
| Role `debezium` | CNPG `managed.roles` on `shop-db` (sf-platform; password in Secret `shop-db-debezium`, keys `username`, `password`); compose init SQL (sf-app, dev password) | `LOGIN REPLICATION`, not superuser, not owner of anything |
| Table `heartbeat` | Alembic (sf-app) | `id smallint PRIMARY KEY CHECK (id = 1)`, `beat_at timestamptz NOT NULL DEFAULT now()`; one row (`id = 1`) inserted by the migration |
| Schema `meta`, table `meta.cdc_epochs` | Alembic (sf-app) | `epoch integer PRIMARY KEY`, `started_at timestamptz NOT NULL DEFAULT now()`, `snapshot_completed_at timestamptz NULL`; written by `scripts/cdc-epoch.sh` (sf-data) as `shop_app` |
| Publication `shop_cdc` | Alembic (sf-app) | `FOR TABLE` the 5 shop tables + `heartbeat` (explicit list, never `FOR ALL TABLES`; `meta` is never published) |
| Grants to `debezium` | Alembic (sf-app) | `USAGE` on schema `public`; `SELECT` on every published table; `UPDATE` on `heartbeat` |

- Debezium (sf-data): `publication.name=shop_cdc`, `publication.autocreate.mode=disabled`, slot `debezium_shop`,
  `heartbeat.action.query=UPDATE heartbeat SET beat_at = now() WHERE id = 1`.
- The migration fails if role `debezium` does not exist; the migration Job's retries cover the short window
  before CNPG reconciles managed roles.
- Other database roles on `shop-db` (CNPG `managed.roles` by sf-platform, password Secret in namespace `shop`):

  | Role | Secret (`shop`) | Grants / ownership | Consumer |
  |---|---|---|---|
  | `trino_pg` | `shop-db-trino-pg` | Alembic (sf-app): `USAGE` on `public`, `meta`; `SELECT` on the 5 shop tables, `heartbeat`, `meta.cdc_epochs`; nothing else | Trino catalog `pg` (sf-data) |
  | `polaris` | `shop-db-polaris` | owns Database `catalog` (CNPG `Database`, sf-platform) and its schema `polaris_schema`; no access to `shop` | Polaris (sf-data) |
  | `airflow` | `shop-db-airflow` | owns Database `airflow` (metadata); no access to other databases | Airflow (sf-data) |
  | `flink_serving` | `shop-db-flink-serving` | owns Database `serving`; creates and upserts `kpi_minute` (DDL by sf-data's init Job) | Flink (sf-data) |
  | `grafana_serving` | `shop-db-grafana-serving` | `CONNECT` on `serving` + `SELECT` on its tables (granted by `flink_serving`); read-only | Grafana KPI datasource (sf-data) |
  | `fulfillment_worker` | `shop-db-fulfillment-worker` | Alembic (sf-app, 0004): `USAGE` on `public`; `INSERT` on `shipments`; column `SELECT (id, status)` on `orders` (a shipment is created only while the order is currently `paid`, so stale events after a restore are skipped); nothing else | fulfillment-worker (sf-app, Phase 7) |
- `pg_hba` confines each role above to its own database (as for `debezium`/`trino_pg`/`polaris`). Consumers in other
  namespaces get their copies through copy Jobs (gitops.md §5). The serving datasource and KPI dashboard are
  sf-data objects in `observability` (sf-sre reviews).
- `debezium` has `REPLICATION`: it could open its own logical slot with another output plugin and read changes of
  every table, whatever the publication and grants say. Treat Secret `shop-db-debezium` (and any copy for Kafka
  Connect) as a database-wide read credential: limit who can read it.
- Adding a source table = one migration that creates it, adds it to `shop_cdc`, grants `SELECT` to `debezium` and `trino_pg`,
  plus a file in `data/contracts/`. The contract check fails if published tables and contract files differ.

## Images

- `ghcr.io/winthebest/shopflow-<service>` for `gateway`, `orders`, `payments`.
- Multi-arch: `linux/arm64`, `linux/amd64`.
- Tags: `sha-<short git sha>` (immutable); deploy manifests pin by digest once available. No `latest`.
- Until the first images are pushed, the chart may use a placeholder image with the same ports and health paths.

## Kubernetes

- Namespace `shop` for services, DB and migration Job.
- Service names = service names above; ClusterIP; ports as above.
- Pods carry `app.kubernetes.io/name: <service>` and `app.kubernetes.io/part-of: shopflow` (the log pipeline maps
  `app.kubernetes.io/name` to `service.name`).
- Only `gateway` gets an `HTTPRoute`. Local host: `shop.127.0.0.1.sslip.io`.
- Pod lifecycle: `preStop` sleep ~5s (endpoints are removed before uvicorn closes its socket),
  `terminationGracePeriodSeconds: 30` (5s + 20s drain + margin); readiness probe `timeoutSeconds: 2`.
