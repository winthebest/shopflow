# Runbook: CheckoutAvailabilityBurn

| | |
|---|---|
| Alert | `CheckoutAvailabilityBurn` (`severity=page` or `severity=ticket`) |
| SLO | 99.5% of `POST /checkout` do not return 5xx, 28-day window ([docs/slo/checkout.md](../slo/checkout.md)) |
| Rules | generated from [`slo/checkout.yaml`](../../slo/checkout.yaml) |
| Dashboards | Grafana → Shopflow → *SLO – checkout*, *RED – shop services* |

## What it means

The gateway answers `POST /checkout` with 5xx faster than the error budget allows.

- **page**: 14.4× budget burn over 1h (confirmed over 5m) or 6× over 6h (confirmed over 30m). With every
  checkout failing, the page fires after about 4 minutes. Act now.
- **ticket**: 3× over 1d (confirmed over 2h) or 1× over 3d (confirmed over 6h). Handle in the next working
  session.

A page for the same SLO silences its ticket (Alertmanager inhibit rule).

## Triage (first 5 minutes)

1. Open Grafana: `kubectl -n observability port-forward svc/kps-grafana 3000:80` → <http://localhost:3000>.
2. *RED – shop services* → *Requests/s by route and status*: which status codes? Per
   `docs/contracts/services.md` (*POST /checkout responses*):
   - **503** `payments unavailable (circuit open)`: the circuit breaker in orders is open ([ADR 0102](../adr/0102-resilience-patterns-checkout.md)):
     at least 75% of the last 10 s of payments attempts (≥ 20) failed, so orders refuses checkouts for 5 s
     before creating the order, then lets one probe through. The cause is payments; look there.
   - **504**: the orders → payments 800ms deadline ran out, after up to 3 attempts (each at most 500ms), or
     orders timed out (gateway → orders 1s).
   - **502**: payments or orders unreachable / bad answer, or orders returned a 5xx (including database errors).
   - Declined payments are `201` (order `failed`) and never burn this budget.
3. *Outgoing call errors/s*: which hop fails (gateway → orders, orders → payments, orders → Postgres)?
   For orders → payments, *SLO – checkout* → *Payments resilience*, or Explore:
   - `sum by (attempt, outcome) (rate(orders_payments_attempts_total[5m]))`: attempts by number (1–3) and
     outcome. Attempts 2–3 rising means retries absorb a partial fault; every attempt failing means payments is
     down or slow everywhere.
   - `orders_payments_circuit_state`: 0 closed, 1 half-open, 2 open, per orders process.
   - `rate(orders_payments_circuit_rejected_total[5m])`: checkouts refused with 503 while the circuit is open.
4. Grafana Explore → Tempo, search `service.name=gateway`, `status=error`, route `/checkout` → open a failing
   trace → *Logs for this span* for the error message (Loki, same `trace_id`).
5. Kubernetes state: `kubectl -n shop get pods,cluster` and recent events
   (`kubectl -n shop get events --sort-by=.lastTimestamp | tail`).

## Common causes and mitigation

| Cause | Check | Mitigation |
|---|---|---|
| Payments slow or failing on every pod → circuit opens → fast 503s (504s while it closes again) | `orders_payments_circuit_state` = 2; attempts failing at every attempt number; `PAYMENT_LATENCY_MS` on the payments Deployment | Fix payments (revert the value in git; Argo CD syncs it). The circuit closes by itself on the next successful probe |
| One payments pod slow or restarting → mostly absorbed by retries | `attempt="2"` rising, circuit closed, a few 504s | Find the pod (`kubectl -n shop get pods -l app.kubernetes.io/name=payments`, its logs); delete it if it is stuck |
| orders/payments pods crash-looping or not ready → 502 | `kubectl -n shop get pods`, `kubectl -n shop logs deploy/orders` | Roll back the last image digest in git |
| Postgres primary down / failover in progress | CNPG dashboard; `kubectl -n shop get cluster shop-db` | Wait for CNPG failover; if stuck, follow the restore runbook (`docs/runbooks/restore.md`) |
| Timeouts after a latency incident | `CheckoutLatencyBurn` firing too | Fix latency first (see [checkout-latency.md](checkout-latency.md)) |

## Blind spot

The SLI comes from the gateway's own server spans. If **all gateway pods are down**, no spans are produced and
this alert cannot fire; `CheckoutSLIMissing` ([checkout-sli-missing.md](checkout-sli-missing.md)) and the k6
results are the signals in that case.

## After the alert resolves

Write a postmortem in `docs/postmortems/` when the incident consumed **≥ 20% of the 28-day budget** or was a
page during a game day (error budget policy in [docs/slo/checkout.md](../slo/checkout.md)).
