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
