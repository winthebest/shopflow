# Runbook: CdcFreshnessProbeDown

| | |
|---|---|
| Alert | `CdcFreshnessProbeDown` (`severity=ticket`), after 10 minutes, held for the first 15 minutes after namespace `lakehouse` is created or the exporter starts |
| Rule | [`deploy/platform/slo/base/cdc-sli.prometheusrule.yaml`](../../deploy/platform/slo/base/cdc-sli.prometheusrule.yaml) |
| Why it exists | A blind SLI must not look green: while the probe fails, every minute counts as bad for `cdc-lag` |

## What it means

`freshness-exporter` (namespace `lakehouse`, docs/adr/0402) could not read `max(_source_ts_ms)` of
`bronze.heartbeat` through Trino for 10 minutes (`freshness_probe_success == 0`), or its metrics are absent.

## Triage

1. Exporter running and scraped? `kubectl -n lakehouse get pods -l app.kubernetes.io/name=freshness-exporter`;
   in Prometheus, `up{job="freshness-exporter"}`. Absent → ServiceMonitor, NetworkPolicy
   (observability → lakehouse:8080), or the pod itself (`kubectl -n lakehouse logs deploy/freshness-exporter`).
2. Probe errors? The exporter logs one JSON line per failed probe:
   `kubectl -n lakehouse logs deploy/freshness-exporter | tail -20`. Typical messages:
   - TLS / certificate errors → Secret `trino-tls` (cert-manager `Certificate trino-tls`) and its `ca.crt`;
   - `401` → Secret `trino-exporter` vs Trino's password file (`trino-password-db`, both from
     `scripts/data-secrets.sh`);
   - `Access Denied` → `rules.json` in `deploy/platform/trino/base/values.yaml` (`exporter` reads `lake_ro`
     schemas `bronze`, `gold`);
   - table not found / catalog errors → Trino → Polaris (`lake_ro` uses principal `trino_lake_ro`, Secret
     `polaris-trino-lake-ro`) or the bronze table was not created (PostSync Job `trino-bronze-tables`).
3. Trino itself up? `kubectl -n lakehouse get pods -l app.kubernetes.io/name=trino`. Trino scaled to 0 on
   purpose (Flink KPI checks, plan.md) makes this alert fire: that is the intended "no false green".

## Mitigation

Fix the broken hop; the alert resolves after the first successful probe. Treat `cdc-lag` as **unknown** while it
fires. If Trino is down on purpose, acknowledge the alert for that window.
