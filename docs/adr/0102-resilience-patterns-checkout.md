# 0102. Checkout resilience: retry inside the 800ms deadline, circuit breaker, idempotent payments

- Status: Accepted
- Date: 2026-10-10
- Lane: sf-app

## Context

Checkout calls payments once, with an 800ms total deadline (`docs/contracts/services.md`); a timeout settles the
order `failed` and answers 504. Game day 1 (`chaos/gd1-payments-latency.yaml`, every payments pod +800ms) makes
every checkout wait 800ms and fail, burning both checkout SLOs: availability (any gateway 5xx) and latency (gateway
span under 300ms, every request counted, errors included).

Retrying a charge was not safe: the payments mock drew a new random outcome and `charge_id` per call, so a retry
after a timeout (whose first attempt may have succeeded) could charge the same order twice.

## Decision

1. **Payments is idempotent per `order_id`**: `charge_id = uuid5(namespace, order id)`, and an order is declined
   when a SHA-256 hash of its id, as a number in [0, 1), is below `PAYMENT_FAILURE_RATE`. Every attempt and every
   replica answer the same for the same order; no state is shared.
2. **Retry inside the deadline** (`services/orders/src/orders/{resilience,payments_client}.py`, own code): up to
   3 attempts, all inside the 800ms deadline; each attempt at most **500ms** and at most what is left; no attempt
   started with less than 100ms left; full-jitter backoff `uniform(0, min(200ms, 50ms·2^(n-1)))`. Retried: attempt
   timeout, transport error, 502/503/504. Not retried: 201, 402 (a decline is a business outcome), anything else
   (a bug does not go away on retry).
3. **Retries open a new connection** (a second httpx client without keep-alive). kube-proxy picks a backend per TCP
   connection; over the pool, a retry may reuse an idle connection to the pod that just timed out. Payments runs
   two replicas locally so there is another pod to reach.
4. **Circuit breaker per orders process**, counting every attempt: opens when, over the last 10s, at least
   **20** attempts were made and **75%** failed; refuses calls for 5s; then lets exactly one probe through, whose
   result closes or re-opens it. Failure = anything but 201/402.
5. **Open circuit → 503 + `Retry-After`, before the order is created** (the first attempt's permit is taken before
   transaction 1, so no `failed` order is left behind). The gateway passes 503 and `Retry-After` through. The
   gateway never retries `POST /checkout`: it is not idempotent and would create duplicate orders.
6. **Metrics** (OTel, OTLP to the collector like the traces; `shopflow_common.telemetry` now installs a
   MeterProvider), low cardinality, never an order id: `orders.payments.attempts{outcome,attempt}`,
   `orders.payments.circuit.rejected`, `orders.payments.circuit.transitions{to}`, `orders.payments.circuit.state`
   (0 closed, 1 half-open, 2 open, per pod). A WARNING log line marks every opening. Every attempt is an httpx client
   span in the trace.

All thresholds are environment settings of orders (`PAYMENTS_ATTEMPTS`, `PAYMENTS_ATTEMPT_TIMEOUT_MS`,
`PAYMENTS_BREAKER_{WINDOW_S,MIN_CALLS,FAILURE_RATIO,OPEN_S}`); the 800ms deadline is a contract, not a setting.

### The two thresholds: retry rescue against retry-induced outage

**Attempt timeout 500ms, not 300ms.** The attempt timeout is what lets a second attempt fit in 800ms, and lower
buys more retries, but anything slower than it fails even when it would have succeeded:

| Attempt timeout | gd1b (1 of 2 payments pods +800ms) | Payments uniformly +350ms (no chaos file) |
|---|---|---|
| 300ms | ~84% succeed (up to 3 attempts: 0.5 + 0.25 + ~0.09); a retried success takes ~350–450ms | **every attempt times out**: 100% fail, then the circuit opens. A degradation becomes an outage |
| **500ms** | **~75% succeed** (one retry after a timeout, ~250ms left); a retried success takes ~550–600ms | unchanged from today: slow successes (the latency SLO burns, availability does not) |
| 800ms (no per-attempt limit) | 50% succeed (no time left to retry after a timeout) | unchanged |

Both retried successes are above the 300ms latency SLO threshold anyway; what the retry saves is availability.
500ms keeps today's behaviour for any payments up to 500ms slow and still rescues half of gd1b's failures.

**Failure ratio 75% over at least 20 attempts, not 50%.** With one of two pods slow, each attempt fails with
probability 0.5 whatever the number of retries, so the attempt failure ratio sits *at* 50%: a 50% threshold would
open (and flap) exactly when retries cope, and turn gd1b's ~75% successes into 503s. At ~15 attempts/s per orders
replica, 75% is 4–6 standard deviations above 0.5, while game day 1 (every attempt fails) still opens the circuit
after ~20 attempts, within 1–2s.

## Expected game-day results

- **gd1** (every payments pod +800ms): latency burn drops: once the circuit is open (~1–2s), every checkout gets a
  503 in milliseconds (under 300ms) except one probe per ~5.5s cycle (5s open, then a probe that times out at 500ms
  and re-opens it; other checkouts are refused while it runs).
  **Availability burn does not drop**: those checkouts are still 5xx. No client-side pattern can make a payments
  that never answers in time succeed.
- **gd1b** (1 of 2 pods +800ms): checkout errors fall from ~50% (one 800ms call) to ~25%; the circuit stays closed.

## Alternatives considered

| Option | Why not |
|---|---|
| tenacity / stamina for retries | `stop_after_delay` stops retrying but does not trim each attempt's timeout to the time left; we would still write the deadline logic |
| pybreaker, aiobreaker, purgatory | pybreaker is sync-first (async only via Tornado); the async ones are barely maintained; the breaker is ~60 lines with a clock tests can drive |
| Retry only errors where the request never left (connect errors) | Safe without idempotency, but cannot retry a timeout: no help for a slow pod |
| Breaker counting whole charges (after retries) | Retries would hide a dependency at 50% failures; per attempt (Retry around the breaker, as resilience4j) sees the real failure rate, so the threshold is set above the partial-failure rate instead |
| Hedged requests (second attempt in parallel after N ms) | Doubles load on a struggling dependency; not needed for these game days |
| Accept the order and charge asynchronously (202, outbox) | The only way to save availability when payments is down; a different checkout contract, out of scope for Phase 7 |

## Consequences

- Positive: a retry can never charge twice; partial payments failures (a slow or restarting pod, a reset
  connection, a transient 5xx) mostly disappear for the user; a payments outage fails fast instead of holding
  connections for 800ms per checkout; circuit state and retries are visible per pod in Prometheus.
- Negative / risks: a decline is now decided by the order id, not drawn per call. The share stays
  `PAYMENT_FAILURE_RATE` (~2% by default, checked over 100,000 ids), but a fresh database declines the same order
  ids in every run (reproducible load tests), and replaying an order gives the same answer. Each orders replica has
  its own breaker, so replicas can disagree for a few seconds. A retried success is slower than the latency SLO
  threshold.
- When to revisit: payments' normal latency approaches 250ms (the retry no longer fits after a timeout); orders
  runs many replicas with little traffic each (20 attempts per 10s per replica may not be reached: lower
  `min_calls` or share the breaker state); or checkout moves to asynchronous charging.
