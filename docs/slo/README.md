# SLOs

| SLO | Spec | Owner | Doc |
|---|---|---|---|
| checkout availability, checkout latency | [`slo/checkout.yaml`](../../slo/checkout.yaml) | sf-sre | [checkout.md](checkout.md) |
| cdc lag | [`slo/cdc.yaml`](../../slo/cdc.yaml) | sf-data | [runbooks/cdc-lag.md](../runbooks/cdc-lag.md) |
| gold freshness | [`slo/gold-freshness.yaml`](../../slo/gold-freshness.yaml) | sf-data | [gold-freshness.md](gold-freshness.md) |

Tooling: Sloth generates multi-window, multi-burn-rate rules at build time ([ADR 0300](../adr/0300-slo-tooling-sloth.md)).
Every SLO uses the 28-day period and the plan's burn-rate factors (14.4/6/3/1) from
[`slo/windows/shopflow-28d.yaml`](../../slo/windows/shopflow-28d.yaml).

## Adding an SLO

1. Write the Sloth spec `slo/<name>.yaml` (`kind: PrometheusServiceLevel`, namespace `observability`). Copy the
   patterns from `slo/checkout.yaml`:
   - denominator `… > 0`, so an idle window gives no sample instead of NaN;
   - `or vector(0)` on an error query whose series may not exist yet;
   - `runbook_url: https://github.com/winthebest/shopflow/blob/main/docs/runbooks/<file>.md` on the alert, and the
     runbook file itself.
2. `make sre-slo` writes `deploy/platform/slo/base/<name>-slo.prometheusrule.yaml` and refreshes that directory's
   `kustomization.yaml` (it lists every `*.prometheusrule.yaml`). Commit both. Hand-written rules (for example a
   missing-data guard) go in the same directory as `<name>-<topic>.prometheusrule.yaml`; run `make sre-slo` again
   so they are listed.
3. Add promtool unit tests in `slo/tests/<name>.test.yaml`. `rule_files` uses the basename with `.rules.yaml`
   (for example `<name>-slo.rules.yaml`, `<name>-<topic>.rules.yaml`): CI extracts each PrometheusRule's `.spec` to
   that name. Prove at least when the page fires, that it stays silent just before, and that idle traffic is not NaN.
4. `make sre-ci` (or just `make sre-slo-drift sre-rules`). CI fails if a generated file is stale or has no spec, if
   the kustomization misses a rule file, if an alert has no existing runbook, or if a unit test fails.
5. Add a row to the table above and a doc next to `checkout.md` (SLIs, thresholds, error budget policy).

Ownership: files of the CDC and gold SLOs (`slo/{cdc,gold}*.yaml`, `slo/tests/{cdc,gold}*.test.yaml`,
`deploy/platform/slo/base/{cdc,gold}-*.prometheusrule.yaml`) belong to sf-data; sf-sre owns the tooling and reviews them
(`docs/contracts/ownership.md`). The rules `kustomization.yaml` is generated, so any lane may commit the output of
`make sre-slo`.

## Lab behaviour: "duplicate sample for timestamp"

On k3d, a few of the chart's default recording rules (`k8s.rules.*`, cAdvisor `irate`/`rate`) sometimes show
`health: err` with `duplicate sample for timestamp …; overrides not allowed`, several groups within about a minute,
then recover on their next evaluation. Cause: Prometheus schedules rule groups by wall clock (`rules/group.go`:
`missed := time.Since(evalTimestamp)/interval - 1`). When the clock of the Docker VM under k3d steps back, a group
evaluates again a timestamp it has already written, and a rule whose value changed in between cannot append. The
first value is kept: no gap, no wrong number. Seen during Gate 2 on 2026-10-10: six errors in one 50-second burst,
`node_timex_sync_status` 0 on both nodes. NodeClock* alerts are disabled locally for the same reason
(`deploy/platform/kube-prometheus-stack/local/values.yaml`). Real nodes with NTP rarely show it.

`make sre-gate-check` reports these as WARN; any other rule error FAILs in shopflow rules (and is a WARN in the
chart's default rules). The 30-day API server availability group is disabled: its `increase30d` cannot be right
with 3 days of retention.
