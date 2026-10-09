# Runbook: KafkaBrokerDown

| | |
|---|---|
| Alert | `KafkaBrokerDown` (`severity=page`), after 5 minutes |
| Rule | [`deploy/platform/slo/base/cdc-sli.prometheusrule.yaml`](../../deploy/platform/slo/base/cdc-sli.prometheusrule.yaml) |
| Signal | kube-state-metrics: `kube_pod_info` vs `kube_pod_status_ready` for the node pool pods of Kafka `shopflow` |

## What it means

A Kafka node pod (`shopflow-dual-role-N` locally, `shopflow-broker-N` on AWS) has not been Ready for 5 minutes.
Locally there is a single node with replication factor 1: CDC stops completely (Debezium cannot produce, the sink
cannot consume) and `cdc-lag` burns. On AWS (3 brokers, RF=3, `min.insync.replicas=2`) one broker down keeps the
pipeline running; two down stop writes.

## Triage

1. Which pod and why: `kubectl -n kafka get pods -l strimzi.io/cluster=shopflow`, then
   `kubectl -n kafka describe pod <pod>` (events: OOMKilled, PVC not bound, node pressure, image pull).
2. Logs: `kubectl -n kafka logs <pod> --previous | tail -100` (KRaft quorum, storage, certificate errors).
3. Operator view: `kubectl -n kafka get kafka shopflow -o jsonpath='{.status.conditions}'` and the Strimzi
   Cluster Operator logs (`kubectl -n kafka logs deploy/strimzi-cluster-operator | tail -50`), for example a
   rolling update stuck on a pod that cannot become ready.
4. Storage: `kubectl -n kafka get pvc` (5Gi locally): a full disk stops the broker.

## Mitigation

- OOMKilled: raise the node pool memory in `deploy/platform/kafka/local/kustomization.yaml` (heap + limit together).
- Full disk: shorten retention of the CDC topics (7 days by default) or grow the volume; bronze keeps the history.
- Lost storage locally: Kafka is disposable here. Recreate it and start a new epoch (`scripts/cdc-epoch.sh new`,
  restart the connectors, `scripts/cdc-epoch.sh wait`): Debezium snapshots again into the new epoch.
