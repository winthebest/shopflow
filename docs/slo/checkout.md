# SLO: checkout

Owner: sf-sre. Spec: [`slo/checkout.yaml`](../../slo/checkout.yaml) → generated rules
[`deploy/platform/slo/base/checkout-slo.prometheusrule.yaml`](../../deploy/platform/slo/base/checkout-slo.prometheusrule.yaml)
(Sloth, [ADR 0300](../adr/0300-slo-tooling-sloth.md)). Dashboard: Grafana → Shopflow → *SLO – checkout*.

## Why checkout

`POST /checkout` is the only request that makes money: it creates the order, charges payments and writes
Postgres in one transaction (`docs/contracts/services.md`). Browsing (`GET /products`) failing is annoying;
checkout failing loses the sale. One user journey, two SLOs.

## SLIs

Both SLIs are measured at the **gateway** (closest point to the user that we instrument), from its server
spans for route `/checkout`. The OTel `span_metrics` connector turns every span into a counter and a
histogram ([ADR 0301](../adr/0301-otel-collector-single-pipeline.md)); Prometheus ingests them over OTLP.

| SLO | Good event | Valid event | Objective | Error budget (28 days) |
|---|---|---|---|---|
| `checkout-availability` | response is not 5xx | every `POST /checkout` | **99.5%** | 0.5% of requests ≈ 3h22m of total outage |
| `checkout-latency` | gateway server span < 300ms | every `POST /checkout` | **99%** | 1% of requests ≈ 6h43m of all-slow |

```promql
# availability: bad / valid
(sum(rate(traces_span_metrics_calls_total{service_name="gateway",span_kind="SPAN_KIND_SERVER",http_route="/checkout",http_response_status_code=~"5.."}[W])) or vector(0))
/ (sum(rate(traces_span_metrics_calls_total{service_name="gateway",span_kind="SPAN_KIND_SERVER",http_route="/checkout"}[W])) > 0)

# latency: (all − under 300ms) / all      (0.3 is an explicit histogram bucket boundary)
(sum(rate(traces_span_metrics_duration_seconds_count{…}[W])) − sum(rate(traces_span_metrics_duration_seconds_bucket{…,le="0.3"}[W])))
/ (sum(rate(traces_span_metrics_duration_seconds_count{…}[W])) > 0)
```

- `> 0` on the denominator: an idle window produces **no sample** instead of NaN. Without it, one idle
  5-minute window (normal on a lab cluster between load tests) makes the 28-day SLI NaN until it ages out.
  Covered by a unit test that fails if the guard is removed.
- `or vector(0)` on the 5xx numerator: the 5xx series only exists after the first 5xx.
- 4xx (`422` invalid input) are user errors, not unavailability; they count as good events. Declined
  payments (`201`, order `failed`) are good events too: the SLI measures the service, not the card.

## Why these thresholds

- **99.5% availability**: the budget is spent only by real faults (timeouts, crashes, database errors). The
  payments mock declines 2% of payments by default (`PAYMENT_FAILURE_RATE=0.02`); a decline is a business
  outcome answered with `201` and `status: "failed"`, never a 5xx (`docs/contracts/services.md`, *POST /checkout
  responses*). Were declines 5xx, the default load alone would burn 4× the budget. 99.9% would leave 40 minutes
  per 28 days, less than one game day; 99% would hide a single node restart.
- **99% under 300ms**: the happy path is gateway → orders → payments (default 50ms) → Postgres commit, well
  under 100ms on the laptop. 300ms leaves room for a slow dependency while staying far from the timeouts
  (orders → payments 800ms, gateway → orders 1s), so the SLO fires before the timeouts turn latency into errors.
  p99, not p50: the tail is what a user retrying a checkout notices.

## Windows

- **28 days**, rolling: four whole weeks, so every weekday has the same weight.
- **1-day view for the lab**: the dashboard also shows the budget computed over the last 24h, because a lab
  cluster is not up for 28 days and local Prometheus keeps 3 days. The 28-day figures are real maths over
  whatever history exists; README and postmortems state the window that was actually observed. Evidence comes
  from soak tests and game days, not from a 28-day run.
- Sloth computes the 28-day SLI as the mean of the 5-minute ratios (cheap to evaluate). Under the steady k6
  load used here it equals the request-weighted ratio; under very uneven traffic it is an approximation.

## Alerts (multi-window, multi-burn-rate)

Burn rate = observed error ratio / allowed error ratio. At burn rate 1 the budget lasts exactly 28 days.

| Severity | Burn rate | Long window | Short window | Budget used when it fires | Route |
|---|---|---|---|---|---|
| page | 14.4× | 1h | 5m | 2.1% | Discord now, repeat 1h |
| page | 6× | 6h | 30m | 5.4% | Discord now, repeat 1h |
| ticket | 3× | 1d | 2h | 10.7% | Discord, grouped 30m, repeat 24h |
| ticket | 1× | 3d | 6h | 10.7% | Discord, grouped 30m, repeat 24h |

Delivery and access (local overlay, SOPS + KSOPS):

- Alertmanager posts to the Discord webhook in Secret `alertmanager-webhook`
  ([`…/local/secrets/alertmanager-webhook.enc.yaml`](../../deploy/platform/kube-prometheus-stack/local/secrets/alertmanager-webhook.enc.yaml)).
  It ships with a placeholder (`discord.invalid`, never resolves): alerts still show in Grafana and the
  Alertmanager UI, but nothing is delivered until the owner sets the real URL with
  `sops deploy/platform/kube-prometheus-stack/local/secrets/alertmanager-webhook.enc.yaml`.
- Grafana admin password (random, never displayed): copy it without printing it,
  `kubectl -n observability get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d | pbcopy`.

The long window proves the burn is significant; the short window makes the alert stop soon after the fix.
Factors are exactly 14.4/6/3/1 (custom 28-day window file [`slo/windows/shopflow-28d.yaml`](../../slo/windows/shopflow-28d.yaml)).

Detection times, from the promtool unit tests in [`slo/tests/checkout.test.yaml`](../../slo/tests/checkout.test.yaml)
(100 req/s, 1-minute rule evaluation; an alert may lag its recording rules by one evaluation):

| Scenario | Expected | Unit test |
|---|---|---|
| every checkout slower than 300ms (payments at 600ms) | page after 0.144 × 60 = 8.6 min | silent at +8m, page by +10m |
| every checkout returns 503 | page after 0.072 × 60 = 4.3 min | silent at +3m, page by +5m |
| 15-minute slowdown, then fixed | page resolves after the 5m window clears | page at +15m, nothing at +22m |
| 4% of checkouts slow (4× burn), sustained | ticket after 0.03/0.04 × 24h = 18h, never a page | silent at +17h, ticket at +19h |
| lab idle for an hour | no NaN, budget stays visible | 28-day budget remaining = 100% |
| no checkout series at all | `CheckoutSLIMissing` after 15 min | silent at 14m, ticket at 16m |

Each alert links to its runbook: [availability](../runbooks/checkout-availability.md),
[latency](../runbooks/checkout-latency.md), [SLI missing](../runbooks/checkout-sli-missing.md).

## Error budget policy

Budget = what is left of the 28-day budget (dashboard: *Error budget left, 28-day window*).

| Budget left | What we do |
|---|---|
| > 50% | Normal work. Game days on the checkout path are allowed. |
| 25–50% | Changes to gateway/orders/payments/shop-db need a written rollback step in the PR. Reliability items from postmortems go first. |
| < 25% | Freeze feature changes on the checkout path; only reliability fixes. No chaos experiments on checkout. |
| exhausted | Freeze until the 28-day budget is positive again; the next work item is the top cause from the postmortems. |

- Any single incident that burns **≥ 20% of the budget**, and every page during a game day, gets a postmortem
  in `docs/postmortems/` with the budget consumed.
- A game day spends budget on purpose. It is still counted, so the policy above decides whether the next game
  day can run.
- While `CheckoutSLIMissing` fires, the SLOs are **unknown**, not green.

## Known limitations

- **Gateway-side SLI**: when orders, payments or Postgres fail, the gateway still answers (502/504) and still
  emits its server span, so those failures are counted (gateway readiness does not depend on orders). Requests
  that never reach a gateway pod (all gateway pods down, Envoy Gateway errors) are invisible to this SLI;
  `CheckoutSLIMissing` and the k6 results cover the "all gateway pods down" case.
- Declined payments are not visible in span metrics (all `201`); a decline-rate panel would need a business
  metric from the app.
- **100% trace sampling is required** (`docs/contracts/services.md`): the SLI is derived from spans. Any future
  trace sampling must happen after the span_metrics connector in the Collector pipeline.
- The SLI depends on the otel-gateway Collector; when it is down the SLI is missing (and the guard fires),
  never silently good.

## Measured results

To be filled in on the cluster (wave 2): time from `PAYMENT_LATENCY_MS=600` to the page (Phase 3 step 7),
2-hour k6 soak at constant load with dashboard screenshots (step 8). Only measured numbers go here.
