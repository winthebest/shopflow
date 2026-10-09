# Contract: shop services

Owner: orchestrator. Changes go through a PR reviewed by the orchestrator, because `sf-app` (code) and
`sf-platform` (Helm chart) both depend on this file.

## Services

| Service | Port | Purpose | Calls |
|---|---|---|---|
| `gateway` | 8000 | Public API: `GET /products`, `POST /checkout`, `GET /orders/{id}` | `orders` |
| `orders` | 8001 | Creates orders + order_items in one transaction, calls payments, sets status `pending → paid \| failed` | `payments`, Postgres |
| `payments` | 8002 | Mock payment provider with configurable latency/failure | — |

Timeouts: gateway → orders 1s, orders → payments 800ms.

## Common runtime contract

- **Health**: `GET /healthz` (liveness, no dependencies) and `GET /readyz` (readiness, checks downstream/DB).
- **Shutdown**: handle SIGTERM, stop accepting requests, drain in-flight requests within 20s.
- **Logs**: JSON lines on stdout, one object per line, with these keys (exact names):
  `timestamp` (RFC 3339, UTC), `level` (`DEBUG|INFO|WARNING|ERROR`), `message`, `service`, `trace_id`,
  `span_id` (32/16 lowercase hex, empty string when no active span). Extra keys are allowed.
- **Container**: non-root UID/GID `10001`; works with `readOnlyRootFilesystem: true` (only `/tmp` writable).
- **Telemetry**: OpenTelemetry SDK. Until Phase 3 lands: `OTEL_SDK_DISABLED=true`. After Phase 3:

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
| orders | `DATABASE_URL` | from Secret `shop-db-app` key `uri` (CNPG managed role `shop_app`) |
| orders | `PAYMENTS_URL` | `http://payments.shop.svc:8002` |
| payments | `PAYMENT_LATENCY_MS` | `50` |
| payments | `PAYMENT_FAILURE_RATE` | `0.02` (0–1) |
| all | `LOG_LEVEL` | `INFO` |

## Database

- CNPG `Cluster` name `shop-db`, namespace `shop`, database `shop`, owner role `shop_app`.
- Schema is owned by Alembic in `services/orders/migrations/` (only owner of the schema).
- Migration runs as a Kubernetes Job before the services roll out: image = orders image, command `["migrate"]` (wraps `alembic upgrade head`).
- `wal_level=logical` from day 1 (CDC in Phase 4).

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
