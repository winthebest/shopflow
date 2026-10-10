# Performance baseline: shop on docker compose

The reference point for later phases (SLO targets in Phase 3, the "before" of resilience fixes in Phase 7). Numbers
below are measured, not estimated. Re-run and add a dated section whenever the app, the payments config or the
hardware changes; never edit old numbers.

## How to reproduce

```bash
make dev-reset && make dev          # fresh Postgres, migrate, seed, 3 services
make app-loadtest                   # constant 20 checkouts/s for 5m (LOADTEST_RATE, LOADTEST_DURATION, LOADTEST_PROFILE)
make dev-down
```

`loadtest/checkout.js`: each iteration is one browse (`GET /products`) and one checkout (`POST /checkout`, random
customer of the 100 seeded, 1–3 distinct products, quantity 1–3), driven by a `constant-arrival-rate` executor so
the rate does not drop when the system slows down. So **20 checkouts/s = 40 HTTP requests/s** on the gateway.
Every acknowledged order (HTTP 201) is written to `out/acks-<UTC stamp>.jsonl` as
`{"ack": {"order_id", "status", "acked_at"}}` for the RPO measurement in Phase 7; the k6 summary goes to
`out/k6-summary-<stamp>.json`.

## 2026-10-09: 20 checkouts/s, constant, 5 minutes

### Setup

| Item | Value |
|---|---|
| App version | `main` at `3a25922` (PR #8), images built locally by `make dev` |
| Host | MacBook Pro M4 Pro, 12 cores, 24GB, macOS 15.6.1 |
| Docker | Docker Desktop, engine 29.6.1, VM 10 vCPU / 15.8GiB; compose v5.3.0 |
| Other load on the VM | another lane's k3d cluster (5 containers) was running |
| Load generator | k6 v2.3.0 on the host, against `http://localhost:8000` |
| Payments mock | `PAYMENT_LATENCY_MS=50`, `PAYMENT_FAILURE_RATE=0.02` (compose defaults) |
| Telemetry | SDK on, exporter `none` (trace ids in logs, no export) |
| Window | 2026-10-09 11:11:41Z → 11:16:41Z |

### Results (client side, k6)

| Endpoint | Requests | p50 | p95 | p99 | max | Errors |
|---|---|---|---|---|---|---|
| `POST /checkout` | 6,000 | 61.4ms | 72.2ms | 105.2ms | 566.5ms | 0.00% |
| `GET /products` | 6,001 | 4.3ms | 7.4ms | 20.6ms | 559.0ms | 0.00% |
| Iteration (browse + checkout) | 6,000 | 66.3ms | 80.1ms | 127.1ms | 891.3ms | — |

- Throughput held at **20.00 checkouts/s** (40.0 req/s); 0 dropped iterations; at most 11 VUs in flight.
- Outcomes: **5,878 paid, 122 failed (2.03%)**, matching `PAYMENT_FAILURE_RATE=0.02`. Declines are `201` with
  `status: "failed"`, so they are not HTTP errors.

### Where the checkout time goes (server side, from the JSON access logs)

| Hop | p50 | p95 | p99 | max |
|---|---|---|---|---|
| gateway `POST /checkout` | 60.7ms | 70.6ms | 101.5ms | 562.1ms |
| orders `POST /orders` | 59.3ms | 68.3ms | 96.7ms | 492.4ms |

At the median: ~50ms is the configured payments latency, ~9ms is orders' own work (two short transactions, one
read-back, the payments call over HTTP), ~1.4ms is the gateway hop, ~0.7ms is the client/Docker port forward.

### Resources (docker stats, 4 samples one minute apart)

| Container | CPU (of one core) | Memory |
|---|---|---|
| orders | 16–20% | 74–79MiB |
| gateway | 6–20% | 48–49MiB |
| payments | 2% | 43–44MiB |
| postgres | 3–5% | 34–58MiB |

### Correctness checks after the run

- 6,000 ack lines; every acked `order_id` exists in `orders` (0 acked-but-missing).
- `orders`/`payments` agree: 5,878 `paid`/`succeeded`, 122 `failed`/`declined`; **0 orders left `pending`**.
- 0 `ERROR` log lines in gateway and orders.

### Reading the numbers

- The checkout latency is dominated by the payments mock (50ms of ~61ms). The app overhead (~11ms at p50) is the
  number to watch when code changes; set `PAYMENT_LATENCY_MS=0` to measure it directly.
- The ~0.5s maxima come from one episode at 11:14:12Z: within one second, 24 gateway requests over 150ms, on both
  endpoints and in all three services at once. That pattern points at a pause of the shared Docker VM, not at one
  code path; outside it there were only 8 isolated requests over 150ms in 5 minutes, and p99 stays ≈100ms.
- At 20 checkouts/s the stack is far from saturation (orders, the busiest process, uses ~20% of one core), so
  this is a latency baseline, not a capacity limit. Capacity on EKS is a separate, optional measurement
  (Phase 7, `docs/capacity.md`).
- Compose is not the cluster: no Envoy, no network policies, one replica each. Phase 3 SLOs should be checked
  against k3d numbers, using this table only as the floor.

## 2026-10-10: k3d, fulfillment-worker and KEDA (profiles core, obs-lite, data, ops)

### Setup

- Cluster `sf-app` (k3d, 1 server + 1 agent; Docker 15.8 GiB on the laptop), revision `e530e7c`, images `sha-c35f922`.
  Local replicas: gateway 1, orders 1, payments 2; fulfillment-worker 1–3 (KEDA Kafka scaler, `lagThreshold` 50,
  polling 15 s, scale-down stabilization 60 s), `SHIPMENT_LATENCY_MS` 20.
- `make app-worker-check CLUSTER=sf-app [LOAD_RATE=… LOAD_DURATION=… SCALE_TARGET=…]`: k6 (`loadtest/checkout.js`)
  through the gateway over HTTPS; replicas, consumer lag and committed offsets sampled every ~5 s. A lag record is one
  change event of `orders`, about 2 per checkout (created `pending`, then `paid` or `failed`).

### Worker scaling under load

| | 80 checkouts/s, 3 min | 60 checkouts/s, 2 min |
|---|---|---|
| Scale-up | 1 → 3 ready replicas in 19 s | 1 → 2 in 19 s (peak 3) |
| Max consumer lag | 449 records | 311 records |
| Back to 1 replica after the load | 23 s | 41 s |
| Throughput per replica under backlog | 103.5 records/s (2 busy intervals) | 87.8 records/s (1 interval) |
| Shipments / duplicates after the run | 13,787 / 0 | 20,839 / 0 |

During the 80/s run the replicas settled at 2 after ~70 s: ~144 records/s in (28,182 committed in 196 s) against
2 × 103.5 out.

### Checkout at the same time (k6, through the gateway)

| | 80 checkouts/s | 60 checkouts/s |
|---|---|---|
| Checkout errors | 4.06 % | 0 % |
| Checkout p95 / p99 | 746 / 1,007 ms | 244 / 509 ms |
| All requests p50 / p95 / p99 | 65 / 576 / 1,003 ms | 61 / 129 / 410 ms |

80/s is beyond what one local orders replica serves: its SQLAlchemy pool (10 + 10 overflow, 0.5 s wait) ran out, and
426 requests failed waiting for a connection: 156 `GET /products`, 127 before the order was created, 94 at settle and
49 at the read after settle. The gateway answered 504 (orders slower than 1 s) or 502. The payments circuit never
opened. Two of those failure points lose correctness, not only availability:

- **94 orders charged but left `pending`.** Payments answered `succeeded` for all of them, the settle transaction
  could not get a connection, and nothing settles a `pending` order again: no payment row, no shipment.
- **49 paid orders answered with a 5xx** (the read after settle failed): a client that retries creates a second order.

Follow-ups (sf-app): idempotent settle plus a sweeper that re-settles stale `pending` orders (payments is idempotent
per order, so re-charging is safe), no read after settle, then autoscaling and pool sizing for gateway/orders.

### Idempotency under a full re-snapshot

- Baseline: 21,339 orders, CDC epoch `1791633369`. New epoch `1791634107`: Debezium restarted without offsets
  ("No previous offsets found") and snapshotted `orders` (21,339 rows) in under a second; `cdc-epoch.sh wait`
  finished 90 s after the connector resumed.
- `REPLAY_BASELINE`: all 21,339 orders came back as snapshot records (`_op = r`) with the new epoch and the worker
  consumed them. Shipments before and after: 20,839; duplicates: 0. One replica throughout: an order that already has
  a shipment costs no simulated carrier latency (`ON CONFLICT DO NOTHING`).
- `make up` again on the same revision: the epoch was kept, no new snapshot, no connector restart; a checkout placed
  afterwards carries the current epoch.

### Backlog after Kafka Connect was down 30 min (game day 3, `WATCH=1`)

Chaos Mesh replaced the Connect pods (12:48:01 → 13:19:09 UTC) while k6 kept 10 checkouts/s; `lagThreshold` was
still 50.

| | Result |
|---|---|
| 1 → 3 ready replicas | 7 s after the backlog appeared |
| Max consumer lag | 34,845 records |
| Lag drained | 131 s |
| Back to 1 replica | 185 s |
| Throughput per replica | 100 records/s (22 busy intervals) |
| Shipments / duplicates | 59,474 / 0; no shipment for an unpaid order |

No event was lost across the stop. The check reported 2 paid orders without a shipment: the newest orders, still in
flight while k6 ran (0 unshipped paid orders older than 60 s). The check now counts only orders paid more than
`SHIP_GRACE_SECONDS` (60) ago and reports the newer ones as in flight.

### lagThreshold from the measurement (ADR 0304)

- Per-replica rate under backlog: 103.5 records/s (80/s run, 2 busy intervals) and 100 records/s (game day 3,
  22 busy intervals); the 60/s run's 87.8 rests on a single interval.
- `lagThreshold` = rate × target drain time per replica ≈ 100 × 30 s = **3,000** (was 50), applied after game
  day 3.
- KEDA asks for ceil(lag / `lagThreshold`) replicas. At 80 checkouts/s one replica falls behind by ~40 records/s, so
  with 3,000 the second replica arrives after ~75 s (19 s with 50) and a third is never needed; `make
  app-worker-check` now expects 2 by default. Game day 3's backlog (34,845 records) asks for 12, capped at 3, at
  once: the burst behaviour above does not change.
