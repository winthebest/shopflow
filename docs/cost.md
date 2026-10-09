# Cost: AWS account, budget and measured $/hour

Owner: sf-cloud. Numbers marked *estimate* come from the plan; numbers marked *measured* come from Cost Explorer
with the **Credit** charge type excluded. Fill the measured columns after each session (Phase 6 step 11).

## Account and deadlines

| Item | Value |
|---|---|
| Plan | **Free plan** today. Upgrade to **Paid** at Phase 6 step 1 (user action), before the Free plan's 6-month limit |
| Created | `<YYYY-MM-DD>` (user fills; less than 2 months before 2026-10-09) |
| Free plan ends | created + 6 months: the account is closed if not upgraded by then |
| Credits valid until | created + 12 months once on the Paid plan; spend beyond the credits is charged to the card |
| Credit | $100 (project cap: total spend before credits ≤ $100) |
| Region | `ap-southeast-1` |

## Guardrails (layer 0, `infra/tofu/modules/cost-guardrails`)

AWS applies credits before Budgets evaluates spend, so a budget that counts credits stays at $0 and never alerts.

| Guardrail | Setting |
|---|---|
| Budget `shopflow-total` | cost budget, **custom window 2026-10-01 → 2027-10-01** (one cumulative period: thresholds never reset at a month or year boundary), `include_credit = false`, `include_refund = false`, limit $100 |
| Emails | actual > $10, actual > $20, forecast > $25 |
| Budget Action | actual > $25 → attaches `shopflow-budget-deny` (no new EKS cluster/node group, EC2, EBS, ELB, NAT, EIP) to `shopflow-operator` and the LB controller role. Teardown, scale-down and reads keep working. Deliberate checkpoint: review this file, then raise the threshold (`action_threshold_usd`) and reset the action |
| Cost Anomaly Detection | services monitor (AWS's default one is reused when present), each anomaly ≥ $1 sent immediately to SNS `shopflow-alerts` |
| Cost allocation tags | `project`, `env` (activated after the first session; tags must appear in billing data first) |
| Lease + kill switches | the real stop: Budgets refresh only every 8–12 h. Lease default 4 h (max 8 h), GitHub reaper hourly, Lambda reaper from lease + 1 h |

Tags reach controller-created resources too: launch template `tag_specifications` (instances, volumes, ENIs),
EBS CSI `extraVolumeTags`, vpc-cni `ADDITIONAL_ENI_TAGS`, LB controller `--default-tags`.

## SSM parameter types

- Control values (`/shopflow/aws/control/*`: lease, backup pointer, initdb marker, session, resume size; and
  `/shopflow/aws/kafka/cdc-epoch`) are **String**: they are not secret, and the reapers can read the lease
  without `kms:Decrypt`.
- **Every secret is SecureString** (default `aws/ssm` key), written only by `scripts/aws-seed-params.sh` from stdin.
  Never put a secret in a String parameter. See ADR 0508.

## $/hour (fill after measuring)

| Item | Unit price (ap-southeast-1) | Full stack *measured* | Paused *measured* |
|---|---|---|---|
| EKS control plane (standard support) | $0.10/h | | |
| Nodes: Graviton spot (types from `cloud-contract.json`) | spot, varies | | — |
| EBS gp3 (node roots + PVCs) | per GB-month | | |
| NLB (Envoy Gateway) | per hour + LCU | | |
| Public IPv4 (nodes, NLB) | $0.005/h per address | | |
| CloudWatch Logs (EKS audit + authenticator) | per GB ingested | | |
| S3, Glue, SSM, Lambda, SNS, Scheduler | ≈ $0 at this scale | | |
| **Total** | | | |

## Budget plan (*estimate*, from plan.md; update with measured $/hour)

| Item | Cloud hours | Estimate |
|---|---|---|
| Layer 0 for up to 12 months | — | $3–8 |
| Phase 6 (integration, measurements, teardown/reaper tests) | ~20 h | $7–10 |
| Phase 7 (EKS game days, restore drills) | ~15 h | $5–8 |
| Phase 8 (Kyverno Enforce, video, interview demo) | ~8 h | $3–5 |
| Reserve (forgotten teardown, orphaned LB, price drift) | — | ≥ $68 |

## Session log

| Date | Session | Hours | Profiles | Cost before credits | Notes |
|---|---|---|---|---|---|
| | | | | | |
