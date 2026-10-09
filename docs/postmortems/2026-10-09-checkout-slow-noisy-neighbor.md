# Postmortem: slow and failing checkouts during the first soak (noisy neighbour on the lab VM)

- Date: 2026-10-09, 22:04–22:38 (UTC+7)
- Environment: lane cluster `sf-sre` (k3d on Docker Desktop, profiles `core,obs`), k6 at 10 checkouts/s
- Severity: ticket (both checkout SLOs); no page
- Author: sf-sre · Status: action items done except the ones marked open

## Summary

During the first 35 minutes of the Phase 3 soak test, three bursts of slow checkouts hit the shop, and the last
one also produced HTTP 504s. The checkout SLO alerts fired as designed: `CheckoutLatencyBurn` (ticket) 1 minute
into the first burst, `CheckoutAvailabilityBurn` (ticket) during the third. The cause was CPU starvation of the
single Docker VM that runs every lab cluster. The operator (sf-sre) ran heavy offline validation containers on
that VM while the soak was running, and Tempo was crash-looping under an undersized memory limit. Nobody was
notified, because the Alertmanager webhook is still the placeholder.

## Impact

| | Value |
|---|---|
| Checkouts in the window (22:02–22:40) | 22,828 |
| Slower than 300ms (latency SLO bad events) | 812 (3.6%) |
| Failed with 5xx (availability SLO bad events) | 315 (1.4%): 307 × 504, 8 × 502 |
| Error budget used, relative to a real 28-day budget at 10 checkouts/s | latency 0.34%, availability 0.26% |
| Worst 5-minute latency SLI | 19% slow (22:36); checkout p99 1.9s |

The 28-day budget shown by the dashboard went negative (−149% latency, −91% availability), because the lab
Prometheus had only about 2 hours of data. The 28-day figures are only meaningful over a long run; this is the
lab limitation documented in [docs/slo/checkout.md](../slo/checkout.md).

## Timeline (UTC+7)

| Time | Event |
|---|---|
| 21:43 | Lane cluster up; two runtime bugs fixed (operator TLS Secret, Grafana OOM: PR #62) |
| 22:01:40 | k6 soak starts (official 2h run, 10 checkouts/s) |
| 22:04 | First burst, 3 minutes into the soak: 5m latency SLI rises to 3% |
| 22:05:09 | **`CheckoutLatencyBurn` ticket fires** (1× burn over 6h and the "3d" window; with 2h of history these windows hold all the data). Alertmanager routes it to `discord`; delivery fails (placeholder URL) |
| 22:16–22:21 | Second burst; Tempo OOMKilled 5 times (22:20–22:24, limit 512Mi) |
| 22:24–22:37 | Operator runs offline checks for another PR on the same VM: `platform-validate`, `make sre-ci` (docker run of otelcol, loki, tempo, promtool, sloth), negative-test renders. Node CPU busy goes from 51% to **81% (22:33)** |
| 22:31–22:38 | Third burst: latency SLI 19%, checkout p99 1.9s. Per hop at 22:35: orders p99 4.9s, gateway 1.9s, payments 179ms. Gateway → orders hits its 1s timeout → 504s (190 in 2 minutes) |
| 22:33:54 | **`CheckoutAvailabilityBurn` ticket fires** |
| 22:41 | `AlertmanagerClusterFailedToSendAlerts` (critical) fires: notifications keep failing (placeholder webhook) |
| 22:45 | Tempo limit raised to 1Gi (`a84c02f`); operator stops all heavy jobs on the VM |
| 22:46 | SLIs back to normal (5m latency SLI < 1%) |
| 22:49 | Operator notices the firing ticket while checking the alert watcher: **detection by a human 44 minutes after the alert** |

## Root cause and contributing factors

1. **Shared CPU.** k3d nodes are containers in one Docker Desktop VM. The CPU-heavy validation containers and a
   second lab cluster ran there too, so the shop's pods were starved. Every hop slowed down at once, while
   payments' own work (179ms) stayed small, which points to scheduling, not application logic.
2. **Tempo crash loop.** 512Mi was not enough for the soak's trace volume (~10 checkouts/s × ~35 spans). Each
   restart replayed the WAL, adding CPU load at the worst moment.
3. **Timeouts turn slowness into errors.** Once orders took more than 1s, the gateway answered 504.

## What went well

- The SLO alerts fired on a real burn within a minute, without any tuning.
- **The SLI kept working while Tempo was down.** span_metrics kept reporting 9.6–10.3 checkouts/s, matching k6's
  10/s, with no double counting. The exporter queue absorbed Tempo's failures, so only traces were lost. This
  confirms the decoupling argued in [ADR 0301](../adr/0301-otel-collector-single-pipeline.md).
- Metrics alone proved the cause: node CPU (USE dashboard), per-hop p99 (RED dashboard), Tempo restarts.

## What went wrong

- No human was paged or ticketed: the Discord webhook is a placeholder, so the alert reached nobody.
- Tempo's memory limit was a guess, not a measurement.
- A measurement run shared its VM with unrelated heavy work.
- `KubePodCrashLooping` did not fire for Tempo: it needs 15 minutes of continuous `CrashLoopBackOff`, and the
  restarts were spread out. A crash-looping observability backend went unalerted.

## Action items

| Action | Owner | Status |
|---|---|---|
| Tempo memory 1Gi / request 384Mi, from the soak measurement | sf-sre | done (`a84c02f`) |
| Grafana memory and operator TLS fixed before the soak | sf-sre | done (PR #62) |
| Runbook: during soak/timing measurements run no heavy containers on the lab VM; check `docker stats` first ([checkout-latency.md](../runbooks/checkout-latency.md)) | sf-sre | done |
| Set the real Discord webhook (`sops …/alertmanager-webhook.enc.yaml`) | project owner | open |
| Alert on restarts of observability components (e.g. > 2 restarts in 30m), with a runbook | sf-sre | open |
| Silence `NodeClockNotSynchronising` on k3d (node clocks come from the Docker VM) | sf-sre | done (local values) |
