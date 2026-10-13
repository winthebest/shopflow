# Postmortem: Game day 1 — payments 800ms slower, before and after the checkout resilience fix

- Runs (UTC+7, 2026-10-14):
  - **before**: 00:23–02:04, fault 01:35:42–01:50:42, revision `fcd7432` (branch `exp/gd1-before-resilience`, never
    merged: main `9de2862` with gateway, orders and payments at `sha-0c7d9a1`, the last images before #163)
  - **after**: 02:04–03:44, fault 03:16:08–03:31:08, revision `9de2862` (main; images `sha-2372d7c`)
- Environment: fresh k3d `sf-sre` per run, profiles core,obs-lite,chaos; k6 10 checkouts/s, no client retries; the same
  schedule both times: warm-up 65 min, fault 15 min, recovery 10 min. Image digests checked per run (`images.txt`).
- Type: game day 1 (scenario 1), measured before and after the fix (Phase 7: "scenario 1 re-run after the fix: lower
  burn rate, with before/after numbers")
- Severity: page in both runs (`CheckoutAvailabilityBurn`); `CheckoutLatencyBurn` paged only before the fix. No
  notification left Alertmanager: the local Discord webhook is a placeholder (see What went wrong).
- Status: action items open
- Validity: 0 host sleeps (`pmset`) in either run; Chaos Mesh generation unchanged inside both fault windows (no
  rollout); no automatic stop; `make down` clean after each run.

## Game day plan (written before the runs)

| | |
|---|---|
| Hypothesis, before (one payments call, 800ms timeout) | Nearly every checkout waits ~0.8 s and gets a 504; its order is settled `failed`. Availability and latency both burn at ~100% (5m burn ≈ 200× and ≈ 100×); both page. |
| Hypothesis, after (retries inside 800ms, breaker, pending + sweeper, HPA) | The breaker opens within 1–2 s: checkouts get a 503 in milliseconds and no order is created; a probe every ~5.5 s ends in a 504 with its order left pending. Availability burns as before (≈ 200×) and pages; latency burns ≈ 2× and does not page. Pending orders age from creation while the sweeper waits for the circuit: `OrdersPendingTooOld` at ~+10 min, resolved 1–2 min after the end, when the sweeper settles them. HPA does not scale (little CPU). |
| What should differ | latency burn and p99 (≈ 0.8 s → ms), 504 → 503, orders `failed` → pending then settled. **Availability burn does not fall**: every payments pod is slow, so no retry can succeed (by design; a partial fault, gd1b, is where retries lower it). |
| Experiment | `chaos/gd1-payments-latency.yaml`: NetworkChaos, delay 800ms ± 50ms on everything leaving the payments pods, 15 min, after 65 min of steady load |
| Why a 65-minute warm-up | The 1h window is all clean traffic, so the 5m/30m/1h burn rates compare run to run. With only ~65 min of SLI history, the 6h/30m pair pages first (availability ~+2 min, latency ~+4 min), earlier than in production. The same holds for both runs. |
| Blast radius | payments pods (namespace shop): checkout fails. Browse, Postgres and observability must not degrade. Kubelet probe replies are delayed too (~1.6–1.7 s against a 2 s timeout); payments must stay Ready. |
| Stop conditions | Failing checkouts are the expected outcome. Automatic: the runner deletes the experiment if browse (GET /products) 5xx over 5 min stays above 1% on two checks in a row. By hand: anything outside shop degrades. |
| Measurement window | Exclusive (no other heavy Docker work); `caffeinate -dims` for the runner's whole life; `pmset -g log` checked. |

## Summary

Payments answered 800 ms late for 15 minutes, once on the images before the resilience fix and once after. The criterion
was a lower burn rate, and the result is split. **Latency burn fell**: the 5m burn at the end of the fault went from
100× to 1.7× and `CheckoutLatencyBurn` did not page. **Availability burn did not fall**: 200× in both runs, and k6 saw
the same checkout failure rate (16.1%). With every payments pod slow, no client-side pattern can make a checkout
succeed. What the fix changes is how checkouts fail: slow 504s became 503s from the open breaker, and orders that would
fail are no longer created. Failed orders created during the fault went from 8,991 to 11.

## Before vs after

| | Before (`sha-0c7d9a1`) | After (`sha-2372d7c`) | Source |
|---|---|---|---|
| /checkout during the fault: 201 / 503 / 504 | 0 / 0 / ≈ 8,920 | 0 / ≈ 8,720 / ≈ 230 | `checkout-responses-per-min.csv` (per-minute rates, summed) |
| Burn at the end of the fault, availability: 5m / 30m / 1h | 200 / 96.3 / 48.1 | 200 / 97.5 / 48.8 | `checkout-burn-*.csv` |
| Burn at the end of the fault, latency: 5m / 30m / 1h | 100 / 48.2 / 24.1 | 1.7 / 1.2 / 0.6 (5m peak 3.7) | `checkout-burn-*.csv` |
| Checkout p99 during the fault: gateway spans (1m) | 0.998–0.999 s | 0.61–0.98 s; 0.61–0.65 s while the breaker was open | `checkout-latency-p99-s.csv` |
| Checkout over the whole run, k6: p99 / median | 825 ms / 67 ms | 108 ms / 66 ms | `k6-summary.json` |
| Checkout failures over the whole run, k6 | 16.10% (8,984 / 55,799) | 16.14% (9,008 / 55,801) | `k6-summary.json` |
| `CheckoutAvailabilityBurn`: ticket / page | +55 s / +2m55s | +1m08s / +2m38s | `alerts.tsv` |
| `CheckoutLatencyBurn`: ticket / page | +1m25s / +4m55s | did not fire | `alerts.tsv` |
| Orders created during the fault, at its end: failed / paid / pending | 8,991 / 318 / 0 | 8 / 454 / 72 | `orders-by-status.txt` |
| The same orders after the 10-minute recovery | 8,991 / 318 / 0 | 11 / 523 / 0 | `orders-by-status.txt` |
| Oldest pending order at the end of the fault | none pending | 873 s (metric, 03:31:01); 441 s (SQL, 03:31:42) | `orders-pending-oldest-age-s.csv`, SQL |
| `OrdersPendingTooOld` | cannot fire (no metric in these images) | ticket +10m23s, resolved 36 s after the runner saw the fault end | `alerts.tsv` |
| Payments circuit (per-minute samples in the fault) | no metric | open 12, half-open 3 | `payments-circuit-state.csv` |
| HPA replicas (gateway, orders) / payments Ready pods | 1, 1 / 2 throughout | 1, 1 / 2 throughout | `shop-replicas.csv`, `payments-ready-pods.csv` |

Fault times are relative to the inject. The SQL's "created during the fault" ends when the runner saw the experiment
stop, about 31 s after the delay was lifted. Both runs therefore include about 300 orders from normal traffic: they are
the 318 paid orders "before", and most of the paid ones "after". Outside the fault, about 880 orders failed in each run.
That matches payments declining 2% of orders by design (`PAYMENT_FAILURE_RATE`).

The p99 "after" is not milliseconds because about 2.5% of the fault's checkouts are breaker probes that wait for
payments and end in a 504. That is more than 1%, so the p99 lands among them (interpolated inside a histogram bucket).
k6's median stayed at 66 ms.

Not measured: how many "before" charges reached payments. Charges that reached it were taken on time, while orders gave
up and marked the order `failed`. With a real provider, that is money taken for failed orders. The mock keeps no record
of charges, and not every charge got there. The delay applies to every packet leaving payments, including the handshake
reply. Orders' 0.8 s timeout applies to each phase, and httpx drops a connection after a read timeout, so later charges
opened new connections. Those spent about 0.8 s on the handshake, and some timed out before being sent. The money risk
is why ADR 0102 made charges idempotent and leaves an unanswered order pending until it can be settled. "After", the
sweeper settled the 72 pending orders within 2 minutes of the fault ending (about 165 settlements in the first minute).

## Timeline (UTC+7)

| Time | Run | Event |
|---|---|---|
| 00:23:03 | before | Runner starts; `make up` ready at 00:30:32 (448 s); k6 from 00:30:37 |
| 00:40:38 | before | Warm-up gate check reaches the wrong context (`k3d-sf-main`): no result (see What went wrong) |
| 01:35:42 | before | Delay applied |
| 01:36:37 | before | `CheckoutAvailabilityBurn` ticket; latency ticket 01:37:07 |
| 01:38:37 | before | `CheckoutAvailabilityBurn` page |
| 01:40:37 | before | `CheckoutLatencyBurn` page |
| 01:49:37 | before | `AlertmanagerFailedToSendAlerts` (warning) and `AlertmanagerClusterFailedToSendAlerts` (critical) |
| 01:50:42 | before | Delay lifted (runner sees the stop at 01:51:13); checkouts back to 201 by the next sample |
| 02:01:17 | before | Recovery check: 0 orders pending over 60 s; `make down` 02:04:01 |
| 02:04:02 | after | Runner starts; `make up` ready at 02:10:56 (414 s); k6 from 02:11:01 |
| 02:21:03 | after | Warm-up gate check: 9 PASS, 0 FAIL |
| 03:16:08 | after | Delay applied; the breaker opens in the first minute |
| 03:17:16 | after | `CheckoutAvailabilityBurn` ticket |
| 03:18:46 | after | `CheckoutAvailabilityBurn` page |
| 03:26:31 | after | `OrdersPendingTooOld` ticket |
| 03:29:46 | after | `AlertmanagerFailedToSendAlerts` and `AlertmanagerClusterFailedToSendAlerts` |
| 03:31:08 | after | Delay lifted (runner sees the stop at 03:31:40); breaker closed by 03:32:01 |
| 03:32:16 | after | `OrdersPendingTooOld` resolved; oldest pending order 0 s by 03:33:01 |
| 03:41:43 | after | Recovery check: 0 orders pending over 60 s; `make down` 03:44:23 |

## Result vs hypothesis

| Prediction | Measured | |
|---|---|---|
| Before: 504s, both SLOs page (availability ~+2 min, latency ~+4 min) | 504s; availability page +2m55s, latency page +4m55s | as predicted |
| After: breaker opens in 1–2 s, fast 503s, no order | open from the first minute; ≈ 97% of fault checkouts were 503s | as predicted |
| After: a probe every ~5.5 s ends in a 504, order pending | ≈ 230 504s in 15 min (one per ~4 s); 72 pending at the end | as predicted, probes a little more often |
| After: availability pages as before | 200× 5m burn, page +2m38s | as predicted |
| After: latency burn ≈ 2×, no page | 5m burn 1.7× at the end (peak 3.7×), no latency alert | as predicted |
| After: `OrdersPendingTooOld` at ~+10 min, resolved 1–2 min after the end | +10m23s; resolved 68 s after the delay was lifted | as predicted |
| After: HPA does not scale | gateway 1, orders 1 throughout | as predicted |
| Payments stays Ready (probes delayed ~1.6–1.7 s vs 2 s timeout) | 2 Ready pods throughout both runs | as predicted |

## What went well

- Every prediction held, including the one that the fix would not lower availability burn under a full fault.
- Browse was untouched: in both runs, k6's failed checks equal its failed checkouts, so no GET /products failed.
  Payments stayed Ready, and HPA did not move.
- Recovery was immediate in both runs: checkouts returned 201 in the first minute after the delay was lifted, and no
  order stayed pending.
- The host guard and the per-minute Chaos Mesh generation log made validity checkable after the fact. The two earlier
  "before" attempts were discarded on that evidence (one stuck Chaos Mesh rollout, one laptop sleep).

## What went wrong

- **No notification was delivered in either run.** `AlertmanagerFailedToSendAlerts` fired about 14 minutes into each
  fault. The `page`, `ticket` and `critical` routes notify Discord, and the local webhook URL is a placeholder (an
  `.invalid` host, until the owner sets the real one with `sops`). The firing times above are Alertmanager state, not
  delivery. The gate check already reports these failures as the placeholder webhook.
- **"Before" has no warm-up gate result.** The runner passed the context in a variable `sre-gate-check.sh` does not
  read, so the check went to its default `k3d-sf-main` and could not reach it. The runner logged that as
  "0 PASS, 0 FAIL". It was fixed between the runs: it passes `--context`, and it now logs a check with no result lines
  or a non-zero exit as FAIL. Warm-up health "before" rests on the samples instead: burn 0 and only 201s from the first
  minute, payments Ready 2, and 20 rows each of checkout burn 5m/30m/1h in the smoke evidence.
- **The SLO line of the "after" gate check passed on nothing.** The runner's tools copy had no `slo/`, so the check
  found no spec and printed `PASS SLO has SLI data 2/ SLOs`. SLI data was present: the smoke evidence has 20 rows
  each of checkout burn 5m/30m/1h, and the samples show burn 0 from the first minute. #260 made the check fail
  when it finds no spec, and the runner now copies `slo/`.
- **The browse stop condition never had a value.** Its query divides the 5xx rate by the total rate. With no 5xx
  series at all, it returns nothing rather than 0, so all 93 samples of each run show it empty. Browse had no errors, so
  the condition could not have triggered. The runner now falls back to 0 (`or vector(0)`) for future runs.

## Action items

| Action | Owner | Status |
|---|---|---|
| Delete `exp/gd1-before-resilience` once this postmortem is merged | sf-app | open |
| `OrdersPendingTooOld` fired as a ticket "after" (72 pending at the end of the fault, all settled by +2 min): decide whether it should stay a ticket while the payments circuit is open, or be inhibited by it | sf-sre, sf-app | open |
| Alertmanager delivery: the local webhook is a placeholder, so no game day has delivered a page. A delivery test needs the real webhook | user decision | open |
| gd1b (`chaos/gd1b-payments-partial.yaml`, one of two payments pods slow): the run that would show retries lowering availability burn | user decision; not approved | open |
| Gate check fails when it finds no SLO spec, with a test (#260); the game-day runner copies `slo/` and its browse query falls back to 0 | sf-sre | done |
