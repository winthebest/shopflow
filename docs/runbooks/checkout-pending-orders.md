# Runbook: OrdersPendingTooOld

| | |
|---|---|
| Alert | `OrdersPendingTooOld` (`severity=ticket`): the oldest pending order is more than 5 minutes old, for 5 minutes |
| Rule | [`deploy/platform/slo/base/orders-pending.prometheusrule.yaml`](../../deploy/platform/slo/base/orders-pending.prometheusrule.yaml) (tests: `slo/tests/orders-pending.test.yaml`) |
| Related | [checkout-availability.md](checkout-availability.md) (503/504 from payments), [ADR 0102](../adr/0102-resilience-patterns-checkout.md) |
| Dashboard | Grafana → Shopflow → *SLO – checkout* → *Payments resilience* |

## What it means

When orders gets no answer from payments (attempt timeouts, payments unreachable, circuit open), it does not guess:
the order stays **PENDING** and the checkout answers 504/502 with status `pending`. The money may or may not have
been collected. A sweeper in every orders process wakes every 10 s, charges stale pending orders again (the charge
is idempotent per `order_id`, so a payment already collected is returned, never taken twice) and settles them as
`paid` or `failed`.

`orders_pending_oldest_age_seconds` is the age of the oldest pending order at the last sweep. Above 5 minutes for 5
minutes, orders are not being settled: customers wait for a status, and fulfillment does not ship them.

## Triage, in this order

1. **Payments circuit open?** `max by (instance) (orders_payments_circuit_state) == 2`, or the orders log line
   `stranded-order sweep paused: payments circuit open`. The sweeper waits on purpose instead of adding load to a
   failing payments. Fix payments ([checkout-availability.md](checkout-availability.md)); the backlog drains by
   itself once the circuit closes: `orders_settle_recovered_total` rises and the age falls.
2. **Sweeper failing?** Orders log `stranded-order sweep failed` (with a traceback), usually the database
   (`kubectl -n shop get cluster shop-db`, CNPG dashboard). While sweeps fail, the gauge keeps its last value: the
   alert stays on until a sweep succeeds.
3. **One order stuck?** The age keeps growing while `orders_settle_recovered_total` rises for other orders: the
   oldest order is claimed again at every lease (30 s) and payments never answers for it. The sweeper logs
   `stranded order: payments did not answer, retrying later` with the `order_id` at each such retry: the same
   `order_id` repeating is the stuck one (the checkout that left it pending logged `payments did not answer, order
   left pending`). `sum by (outcome) (rate(orders_payments_attempts_total[5m]))` shows the timeouts. Then look for
   that order in payments' logs.

Logs: `kubectl -n shop logs deploy/orders --since=30m | grep -E 'stranded-order|did not answer|circuit open|stranded order settled'`.

## Mitigation

| Cause | Action |
|---|---|
| Payments down or slow everywhere (circuit open) | Restore payments; nothing to do on orders. Watch the age fall to seconds |
| Database errors in the sweeper | Fix the database first ([restore.md](restore.md) if it is lost); sweeps resume by themselves |
| A single order payments never answers for | Leave it pending (it is retried every lease) and open an issue for sf-app with the `order_id`; never settle it by hand in SQL without knowing whether the payment was collected |

## After

The alert resolves once the oldest pending order is younger than 5 minutes. During a game day, record the peak age,
how many orders the sweeper settled (`increase(orders_settle_recovered_total[1h])` by status) and how long the
drain took.
