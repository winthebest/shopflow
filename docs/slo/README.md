# SLOs

| SLO | Spec | Owner | Doc |
|---|---|---|---|
| checkout availability, checkout latency | [`slo/checkout.yaml`](../../slo/checkout.yaml) | sf-sre | [checkout.md](checkout.md) |

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

Ownership: files of the CDC SLOs (`slo/cdc*.yaml`, `slo/tests/cdc*.test.yaml`,
`deploy/platform/slo/base/cdc-*.prometheusrule.yaml`) belong to sf-data; sf-sre owns the tooling and reviews them
(`docs/contracts/ownership.md`). The rules `kustomization.yaml` is generated, so any lane may commit the output of
`make sre-slo`.
