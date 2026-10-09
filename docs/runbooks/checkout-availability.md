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
   - **504**: payments timed out (orders → payments 800ms) or orders timed out (gateway → orders 1s).
   - **502**: payments or orders unreachable / bad answer, or orders returned a 5xx (including database errors).
   - Declined payments are `201` (order `failed`) and never burn this budget.
3. *Outgoing call errors/s*: which hop fails (gateway → orders, orders → payments, orders → Postgres)?
4. Grafana Explore → Tempo, search `service.name=gateway`, `status=error`, route `/checkout` → open a failing
   trace → *Logs for this span* for the error message (Loki, same `trace_id`).
5. Kubernetes state: `kubectl -n shop get pods,cluster` and recent events
   (`kubectl -n shop get events --sort-by=.lastTimestamp | tail`).

## Common causes and mitigation

| Cause | Check | Mitigation |
|---|---|---|
| Payments slower than the 800ms timeout (game day, config) → 504 | `PAYMENT_LATENCY_MS` on the payments Deployment | Revert the value in git; Argo CD syncs it |
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
