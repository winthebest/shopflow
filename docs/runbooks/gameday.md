# Runbook: game days (Chaos Mesh)

Phase 7 runs at least five game days. Each one has a written hypothesis, a limited blast radius and stop
conditions **before** it starts, and ends with a postmortem from [TEMPLATE.md](../postmortems/TEMPLATE.md).
Chaos Mesh exists in the cluster only during the game day ([ADR 0303](../adr/0303-chaos-mesh-game-day-only.md)).

## Before

1. Ask the orchestrator for a slot. Announce the **measurement window**: no other lane runs heavy Docker work on the
   VM (`docs/contracts/environment.md`). Keep the laptop awake: lid open, `caffeinate -dims`; check afterwards that
   `pmset -g log` shows no `Sleep` inside the window.
2. Cluster up with the profiles the scenario needs (e.g. `make up CLUSTER=sf-sre PROFILES=core,obs GIT_REVISION=…`).
   The namespaces under test (`shop`, `kafka`) carry `chaos-mesh.org/inject=enabled` (sf-platform's Namespace
   objects); no other namespace can be targeted.
3. **Steady state first**: at least 2h20m of k6 load at a constant rate before an SLO-timing experiment, otherwise
   the 6h windows hold too little history and alerts fire early ([docs/slo/checkout.md](../slo/checkout.md)).
4. Copy TEMPLATE.md to `docs/postmortems/YYYY-MM-DD-gd<n>-<slug>.md` and fill the game day plan.
5. `make sre-chaos-on CLUSTER=… GIT_REVISION=…` → wait until the `chaos-mesh` Application is Synced/Healthy.

## During

1. Note the time, then `kubectl apply -f chaos/gd<n>-….yaml`. Experiments with a `duration` stop by themselves;
   pod-kill experiments are one-shot.
2. Watch: Grafana *SLO – checkout*, *RED*, the alert list (`ALERTS`), and the stop conditions in the file header.
3. Stop early with `kubectl delete -f chaos/gd<n>-….yaml` if a stop condition is met.

## After

1. `make sre-chaos-off CLUSTER=…`: deletes root-chaos, every experiment (while the controller still runs), then
   the Chaos Mesh Application. It fails unless no chaos CRD, daemon pod or webhook is left.
2. Export the graphs/numbers the postmortem needs before the cluster goes away.
3. Write the postmortem; open an action item for every fix; re-run the scenario after the fix and fill the
   before/after table.

## Scenarios

| # | File / steps | Where | Hypothesis (short) |
|---|---|---|---|
| 1 | `chaos/gd1-payments-latency.yaml` (payments +800ms on every pod, 15m) | local | before the fix: nearly every checkout times out (504), availability page by +5m, latency page by +10m; after the fix (retry + circuit breaker): fast 503s cut the latency burn, availability still pages (no pod can answer in time) |
| 1b | `chaos/gd1b-payments-partial.yaml` (1 of 2 payments pods +800ms, 15m), with the resilience fix (ADR 0102) | local | one retry reaches the healthy pod: errors ~50% → ~25%, circuit stays closed, retried checkouts ~550–600ms (latency burns); tickets but no page after 2h20m of steady load. The pool favours the healthy pod: compare measured numbers |
| 2 | `chaos/gd2-kill-postgres-primary.yaml` (shop-db with 2 instances) | local | failover < 30s; 5xx only during the switch; Debezium's logical slot is lost → re-snapshot with a new epoch (`scripts/cdc-epoch.sh`) |
| 3 | `chaos/gd3-debezium-stopped.yaml` (Kafka Connect down 30m) | local | WAL retained by `debezium_shop` grows; the WAL-retained alert fires before `max_slot_wal_keep_size` |
| 4 | Manual: apply a schema-breaking migration outside CI (simulated) | local | silver/dbt break; time to detect is measured; the data-contract gate prevents it |
| 5 | `chaos/gd5-kill-kafka-broker.yaml` (3 brokers, RF=3) | EKS | ISR shrinks, leaders move in seconds, no message loss, full ISR within minutes |
| 6 | Manual: drain + `aws ec2 terminate-instances` one node | EKS | PDBs hold, pods reschedule, the 1-AZ PVC re-attaches |

On EKS, add `chaos` to the session's profiles (cloud-up) and run `make sre-chaos-off SRE_KUBE_CONTEXT=<eks
context>` before `cloud-down`.
