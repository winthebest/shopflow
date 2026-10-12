# 0304. KEDA for autoscaling on Kafka consumer lag

- Status: Accepted
- Date: 2026-10-10
- Lane: sf-sre

## Context

Phase 7 must show event-driven autoscaling: `fulfillment-worker` consumes `shop.public.orders` and should scale
1 → N while there is consumer lag and back to the minimum when it is drained. CPU-based HPA reacts to the
symptom (busy pods), not to the backlog, and a mostly idle consumer with a growing lag would never scale.

## Decision

- **KEDA 2.21.0** (chart and images pinned by digest) as app `keda` in profile `ops`, always on from Phase 7.
  It is the same on k3d and EKS: base values only, and the aws profile lists `apps/keda` directly.
- Certificates come from cert-manager, like Chaos Mesh: the operator neither generates nor patches them at
  runtime, the Secret has a declared source (checked by `sre-lint`), and Argo CD ignores the injected `caBundle`.
- The `ScaledObject` belongs to the worker's deployment (its owner), with the Kafka scaler:
  `bootstrapServers` of the `shopflow` cluster (TLS + SCRAM through a `TriggerAuthentication` that reads the
  worker's read-only KafkaUser Secret), `consumerGroup` of the worker, `topic: shop.public.orders`,
  `lagThreshold` sized from the measured processing rate, `minReplicaCount: 1`, `maxReplicaCount` ≤ the topic's
  partition count (extra consumers would idle). Idempotency (`UNIQUE(order_id)` + `ON CONFLICT DO NOTHING`) makes
  rescaling and re-delivery safe.

## Alternatives considered

| Option | Why not |
|---|---|
| HPA on CPU | Scales on busy pods, not on backlog; a slow consumer with growing lag may never trigger it |
| HPA on an external metric via prometheus-adapter | One more component plus a lag exporter, for what KEDA's Kafka scaler reads directly |
| Strimzi/Kafka-native rebalancing only | Spreads load; does not add consumers |

## Consequences

- Positive: scaling follows the business signal (unprocessed orders); scale-to-min is visible in the game day;
  no extra metrics pipeline.
- Negative / risks: KEDA registers the `external.metrics.k8s.io` APIService (one per cluster; nothing else uses
  it here). Lag-based scaling can flap if `lagThreshold` is too low; set it from a measurement, not a guess
  (game day 3 measured ~100 records/s per replica; sf-app set 3000, about 30 s of work per replica).
- Argo CD applies the KEDA chart's `crds/` without tracking them (seen for Chaos Mesh in game day 3): deleting the
  `keda` Application leaves the `keda.sh` CRDs. Removing KEDA from a cluster means deleting the ScaledObjects first,
  then the Application, then those CRDs by name, as `scripts/sre-chaos-off.sh` does for Chaos Mesh.
- When to revisit: the worker needs scale-to-zero (KEDA supports it; not needed for the demo), or a second
  external-metrics provider is introduced.
