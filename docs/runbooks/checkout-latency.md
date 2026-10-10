# Runbook: CheckoutLatencyBurn

| | |
|---|---|
| Alert | `CheckoutLatencyBurn` (`severity=page` or `severity=ticket`) |
| SLO | 99% of `POST /checkout` complete in under 300ms, 28-day window ([docs/slo/checkout.md](../slo/checkout.md)) |
| Rules | generated from [`slo/checkout.yaml`](../../slo/checkout.yaml) |
| Dashboards | Grafana → Shopflow → *SLO – checkout*, *RED – shop services* |

## What it means

Slow checkouts (≥300ms at the gateway) are using the error budget faster than the SLO allows.

- **page**: 14.4× budget burn over 1h (confirmed over 5m) or 6× over 6h (confirmed over 30m). At 14.4× the
  whole 28-day budget is gone in under 2 days. Act now.
- **ticket**: 3× over 1d (confirmed over 2h) or 1× over 3d (confirmed over 6h). Nothing is on fire, but the
  budget will run out before the window ends. Handle in the next working session.

A page for the same SLO silences its ticket (Alertmanager inhibit rule).

## Triage (first 5 minutes)

1. Open Grafana: `kubectl -n observability port-forward svc/kps-grafana 3000:80` → <http://localhost:3000>
   (admin credentials: Secret `grafana-admin`).
2. *SLO – checkout*, SLO `latency`: which windows are above the threshold lines, and how much budget is left?
3. *RED – shop services* → *Duration: p50/p95/p99 for POST /checkout*: click an exemplar (diamond) above the
   300ms line → the trace opens in Tempo. The slowest span tells you where the time goes:
   - `orders → payments` slow: payments mock or its config (`PAYMENT_LATENCY_MS`).
   - `orders → Postgres` slow: database (CNPG dashboard: connections, locks, replication, disk).
   - `gateway → orders` slow but orders' own spans fast: network, CPU throttling, or the gateway itself.
4. From the trace, *Logs for this span* → the Loki lines with the same `trace_id`.
5. *Outgoing calls p99* panel: is a dependency close to its timeout (orders → payments 800ms,
   gateway → orders 1s)?

## Common causes and mitigation

| Cause | Check | Mitigation |
|---|---|---|
| Payments latency raised (game day, config) | `kubectl -n shop get deploy payments -o yaml \| grep -A1 PAYMENT_LATENCY_MS` | Revert the value in git; Argo CD syncs it |
| Postgres slow / saturated | CNPG dashboard; `kubectl -n shop get cluster shop-db` | Find the slow query or lock; scale resources in git |
| CPU throttling / node saturation | *USE – nodes* (load per CPU > 1); `kubectl top pods -n shop` | Free resources (scale down optional profiles), raise limits in git |
| New release regressed latency | Argo CD history of the `shop` app | Roll back the image digest in git |
| Lab only: the Docker VM is saturated by other containers (CI validators, image builds, another k3d cluster); every hop slows at once while payments' own span stays small | `docker stats --no-stream`; *USE – nodes* CPU busy > 70% | Stop the heavy containers. During soak or timing measurements run nothing heavy on the VM ([postmortem 2026-10-09](../postmortems/2026-10-09-checkout-slow-noisy-neighbor.md)) |

## After the alert resolves

- The page clears about 5 minutes after the fix (the 5m short window), unless the 6h/30m pair still holds.
- Write a postmortem in `docs/postmortems/` when the incident consumed **≥ 20% of the 28-day budget** or was
  a page during a game day (error budget policy in [docs/slo/checkout.md](../slo/checkout.md)).
