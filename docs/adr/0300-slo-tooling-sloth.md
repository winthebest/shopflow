# 0300. Generate SLO rules with Sloth (Pyrra as fallback)

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-sre

## Context

Phase 3 needs multi-window, multi-burn-rate alerts for two checkout SLOs on Prometheus 3, with the plan's
exact policy (page 14.4×/1h+5m or 6×/6h+30m; ticket 3×/1d+2h or 1×/3d+6h; 28-day window), a runbook link on
every alert, and CI that checks the rules offline. Grafana's SLO feature is Cloud-only. The research report
marked both candidates UNVERIFIED, so we ran a 1-hour spike (2026-10-09) with the same SLO in both tools,
`promtool` from Prometheus v3.15.0, and a unit test where every checkout turns slow at t0.

| | Sloth v0.16.0 | Pyrra v0.10.2 |
|---|---|---|
| Last release / activity | 2026-04-04; last commit on main 2026-05-26; a PR bumping the Prometheus library for CVE-2026-42154/42151 open since 2026-09-21 | 2026-09-18; commits daily |
| Prometheus 3 | Plain PromQL output; `promtool check rules` (v3.15.0) OK | Explicit support (`--enable-prometheus-3-migration`, `le` normalisation); `promtool` OK |
| Burn-rate policy | Windows configurable (`AlertWindows`): factors exactly 14.4/6/3/1 | Fixed 14/7/2/1 with 1h, 6h, 1d, **4d** windows; only severities configurable |
| Page time, 100% slow | +9 min (plan expects ~8.6 min) | +11 min (`for: 2m`) |
| Runtime | None: CLI in CI, output is a committed `PrometheusRule` | Operator + API/UI pods for the UI; `generate` CLI also exists |
| Runbook link | `annotations.runbook_url` in the spec | `pyrra.dev/` annotations propagated |
| Missing-data alert | Not generated | `SLOMetricAbsent` built in |

## Decision

Use **Sloth v0.16.0 as a build-time generator only**: `slo/checkout.yaml` + `slo/windows/shopflow-28d.yaml`
→ `make sre-slo` → committed `deploy/platform/slo/base/checkout-slo.prometheusrule.yaml`. CI regenerates and
fails on drift, runs `promtool check rules` and `promtool test rules` (`slo/tests/`). The image is pinned by
digest. The missing-data alert Sloth lacks is a hand-written rule (`CheckoutSLIMissing`).

## Alternatives considered

| Option | Why not |
|---|---|
| Pyrra | Cannot express the plan's burn-rate windows (fixed 14/7/2/1, 4d); a UI pod costs RAM in `obs-lite`; kept as the fallback because its output also passed the spike |
| Hand-written rules | ~30 rules per SLO to keep consistent by hand; the generator + drift check + unit tests give the same review value with less error surface |
| Grafana SLO | Grafana Cloud only |
| OpenSLO spec | Specification only; still needs a generator |

## Consequences

- Positive: zero runtime footprint; rules are reviewable YAML in git; the plan's exact factors; offline tests
  prove alert timing (page 9–10 min at 100% slow, ~4 min at 100% 5xx, ticket after ~18h at 4× burn) before
  any cluster exists.
- Negative / risks: Sloth's release cadence has slowed and a CVE fix in its Prometheus dependency is unmerged.
  Impact is limited to the generator container in CI (never deployed, reads only our spec); it is pinned by
  digest. Sloth computes the 28-day SLI as the mean of 5-minute ratios (approximation under uneven traffic,
  documented in `docs/slo/checkout.md`).
- When to revisit: no Sloth release by 2027-04-01, a generation bug on Prometheus 3, or the CVE PR still open
  when Phase 8 (supply chain) starts → switch to Pyrra `generate` (accepting its fixed windows, recorded in a
  superseding ADR). Generated rules keep working either way.
