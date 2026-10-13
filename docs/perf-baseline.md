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

## 2026-10-12: k3d app slot, 80 checkouts/s with autoscaling, sweeper and Idempotency-Key

### Setup

- Cluster `sf-app` (k3d, 1 server + 1 agent; Docker VM with 10 vCPUs and 15.8 GiB), profiles core, obs-lite, data
  and ops, revision `b0b9333`, images `sha-2372d7c`. HPAs on average CPU per pod: gateway 1–3 at 400m, orders 1–3
  at 500m; payments 2 replicas; orders pool 10 + 5 with a 0.5 s wait; fulfillment-worker 1–3 (KEDA, `lagThreshold`
  3,000).
- Load started more than 10 minutes after `make up`, with the node clocks within 0.2 s of the host. A quiet-host
  check ran around every run: node clock offset, wall-clock steps, Prometheus clock warnings, container churn, macOS
  sleep, and host load sampled every 10 s. Runs 1 and 1b were clean. In run 2, one 15 s step (09:04:10 UTC, during
  the sweeper's drain) had half of the non-shop pods above twice their median CPU; run 2 passed all its checks.
- k6 ran with `RETRIES=1`: a checkout answered 409, 502, 503 or 504 is sent once more with the same
  `Idempotency-Key` after `Retry-After` (else 1 s).

### Correctness: no duplicate order, no duplicate key, every stranded order settled

| | Run 1 (80/s) | Run 1b (80/s, warm) | Run 2 (settle fault, 20/s) |
|---|---|---|---|
| Acked checkouts / distinct order ids | 17,945 / 17,945 | 20,414 / 20,414 | 4,306 / 4,306 |
| Acked after a retry with the same key | 1,884 | 1,161 | 70 |
| Ack's key equals the order's stored key | 100 % | 100 % | 100 % |

Across the slot (53,737 orders): 0 orders without a key, 0 duplicate keys, 0 orders `pending` for more than 60 s,
0 duplicate shipments, 0 paid orders without a shipment; 1,075 declined (2.0 %, payments' simulated rate). A retry
always got back the order of its first attempt, never a second one.

`scripts/app-settle-fault.sh` (run 2) blocked every settle for 20 s at the database after 60 s of load. Payments
still charged and the orders stayed `pending`:

| | Result |
|---|---|
| Orders stranded by the block | 401 |
| All settled by the sweeper | 70 s after the unblock |
| `settle failed` lines with a charge id | 143, each re-settled with that same charge (0 different) |
| k6 retries during the run | 502, none created a second order |
| Blocking constraint after the run | removed |

### 80 checkouts/s: not met on this laptop

| | Run 1: step from 0 to 80/s | Run 1b: warm replicas, then 80/s |
|---|---|---|
| Replicas when 80/s starts | 1 orders, 1 gateway | 3 orders, 2 gateway (3 after 2 min) |
| Checkout first attempts failed | 25.9 % | 13.4 % |
| Checkouts answered 201 (at most one retry) | 82.8 % | 91.8 % |
| `GET /products` answered 200 | 87.3 % | 92.4 % |
| Checkout p99 | 1.46 s | 1.65 s |
| Iterations dropped by k6 (200 VUs) | 2,340 | 1,775 |

Run 1 is `RETRIES=1 make app-worker-check CLUSTER=sf-app LOAD_RATE=80 LOAD_DURATION=5m`. Run 1b first warmed the
HPAs with load: `PROFILE=ramp RATE=60 RAMP_UP=3m` until orders had 3 ready replicas and gateway 2 (216 s). It then
ran `RATE=80 DURATION=5m RETRIES=1` of `loadtest/checkout.js`, and only those 5 minutes count.

The orders 500s in the retained logs were SQLAlchemy `QueuePool` timeouts (0.5 s wait), apart from 11 plain
timeouts; the gateway answered 504 after its 1 s timeout, or 502. The worker side passed in run 1: 1 → 2 replicas
132 s after the load started, max lag 4,978 records, lag drained 18 s after the load ended, back to 1 replica after
54 s, 92.5 records/s per replica (the 3,000 threshold stands), 19,714 shipments with no duplicate.

Two causes:

- **(A) Step load on one replica.** 80/s from t = 0 is beyond one orders replica (about 45/s). The HPAs took about
  100 s to reach 3 orders replicas (2 at 75 s), and the client retries added ~19 requests/s on average to the
  overload: a retry storm. Errors were continuous until 08:53:45 UTC, then came only in bursts. Run 1b, with the
  replicas already up, halved the failure rate.
- **(B) VM CPU saturation at steady state.** CPU was read every 5 s from inside the k3d nodes: each pod's cgroup
  `cpu.stat` and the VM's `/proc/stat`. That is exact, unlike the 30 s Prometheus series. Errors in run 1b follow
  the VM's busy cores:

  | VM busy cores (of 10), per 5 s step | Steps | 5xx | Steps with more than 50 |
  |---|---|---|---|
  | under 5 | 23 | 2 | 0 |
  | 5–6 | 7 | 18 | 0 |
  | 6–7 | 11 | 746 | 3 |
  | 7–8 | 9 | 2,347 | 8 |
  | 8 and more | 5 | 1,482 | 5 |

  The shop alone averaged 3.1 cores (orders 1.62, gateway 0.84, shop-db 0.49, payments 0.19). Periodic platform
  bursts took the VM to 7–9 cores:
  - the Iceberg sink commit, every ~61 s (Kafka Connect up to 0.7 core and Polaris up to 0.6);
  - Argo CD reconciling, up to 1.25 cores;
  - Trino, up to 1 core every ~65 s.

  Four of the five commits fell in error steps, but 11 of the 16 heavy error steps had no commit. The largest
  burst, 771 errors at 09:14:20 UTC, was a Trino spike. In run 1, payments, which has no database, saw its p99 go
  from 60 ms to 130–226 ms during the bursts. So the event loops were starved of CPU and the database was not
  slow: orders held its connections past the 0.5 s pool wait, and the gateway timed out. There was no CPU
  throttling (shop pods have no CPU limit), no steal time reported, and iowait was near 0.

At 80/s this laptop cluster hits the VM CPU ceiling; it is a capacity limit, not an application fault. During run
1b's warm-up, up to 60/s on replicas still scaling gave no 5xx (09:07–09:10 UTC), but only about 36 s ran at a full
60/s: too short to call that the capacity. The exact figure is for the capacity test on EKS.

The same 80/s gave 4.06 % errors on 2026-10-10 with one orders replica, images `sha-c35f922` and no client
retries. Orders then used 881m on one replica (the figure in the chart's `values.yaml`). Today it used 1.62 cores
for about 84 checkouts/s, retries included, plus 74 product reads/s: roughly 1.8× the CPU per request served. This
is not isolated. The candidates are the `Idempotency-Key` read on every checkout, the client retries, and three
processes instead of one; the measurement methods also differ. The 2026-10-13 benchmark below rules out the code.

Mitigations, proposed only (nothing changed):

- `minReplicas` sized for the expected peak: AWS already runs 2, and local 1 is what makes a step load fail;
- a faster HPA `behavior.scaleUp` for step loads;
- client retries with backoff, jitter and a retry budget: k6 retries after `Retry-After` or a flat 1 s, the
  pattern that turned (A) into a retry storm;
- profiling orders' CPU per checkout against `sha-c35f922` before the EKS capacity test (done on 2026-10-13, below).

## 2026-10-13: CPU per checkout, `sha-c35f922` vs `sha-2372d7c`

### Method

- One Python process: gateway → orders → payments over ASGI transports (the integration tests' `shop_client`
  fixture), orders on a real Postgres 17.11 started by testcontainers. Each SHA was extracted with `git archive` and
  synced from its lock (the same lock at both SHAs); the same benchmark test ran at both.
- Each run: 200 warm-up checkouts, then 2,000 `GET /products`, then 2,000 sequential checkouts (one item, payments
  `succeeded` with no latency). Sequential requests leave out pool waits, queueing and the error path, so what is
  measured is the code's own cost per request:
  - CPU is `time.process_time()` of the whole process;
  - SQL statements are counted by a SQLAlchemy `before_cursor_execute` listener;
  - transactions come from `pg_stat_database`.

  OpenTelemetry is not set up in this harness; its code is identical at both SHAs.
- Each repetition interleaves three configurations:
  - `c35f922`;
  - `2372d7c` without an `Idempotency-Key`;
  - `2372d7c` with a fresh key per checkout, as k6 sends it.

  There were 7 repetitions. The Mac was on AC power with its lid open, and did not sleep during the window. It ran a
  background desktop load of about 5–11 on 12 cores, so only ratios within the same repetition are compared.
- Control: `GET /products` did not change between the SHAs, so its CPU per read must agree within ±5 %. After 3
  repetitions it was 6–9 % off (host load rose to 10.8), so 4 more were run.
- Re-run: `scripts/app-bench-checkout-cpu.sh c35f922 2372d7c` (Docker for testcontainers, so never during a
  measurement window; results in `out/app-bench-checkout-*`). The test is
  `services/orders/tests/integration/test_bench_checkout.py`, marked `bench`, and left out of the default and CI
  runs.

### Results

| | `c35f922` | `2372d7c`, no key | `2372d7c`, with key |
|---|---|---|---|
| CPU per checkout, mean | 3.83 ms | 3.62 ms | 3.65 ms |
| Ratio to `c35f922` in the same repetition, median (range) | | 0.94 (0.77–1.17) | 0.98 (0.82–1.07) |
| `GET /products` CPU, mean (ratio median) | 1.00 ms | 1.02 ms (0.99) | 1.02 ms (0.96) |
| SQL statements per checkout | 8 | 6 | 7 |
| Transactions per checkout | 6.0 | 4.1 | 6.0 |

The `2372d7c` code does not use more CPU per checkout than `c35f922`. Both medians are below 1, but both ranges
contain 1.0, so this shows no increase, not a saving. The SQL counts are exact: two statements fewer without a key
(the settle no longer reads the order back), and one more than that with a key (the key lookup).

The roughly 1.8× CPU per request on k3d on 2026-10-12 is therefore not the code, and it is **not explained yet**.
Its candidates are not measured:
- CPU contention on the saturated VM;
- the error path at 13–26 % failed requests;
- three processes instead of one;
- OpenTelemetry export;
- different measurements: the 1.62 cores of 2026-10-12 come from per-pod cgroup counters read every 5 s, while the
  881m of 2026-10-10 is the figure recorded in the chart's `values.yaml` from the ops slot, whose measuring tool was
  not recorded.
