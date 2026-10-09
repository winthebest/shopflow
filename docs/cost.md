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

## $/hour

*Estimates* come from AWS public prices for `ap-southeast-1` on 2026-10-09: the price list API files for EKS, ELB,
VPC and CloudWatch, the EC2 on-demand and EBS price files behind the pricing pages, the public spot price feed, and
the Spot Instance Advisor. No account was used. Spot prices move hourly. Replace the estimates with Cost Explorer
numbers (Credit charge type excluded) after the first sessions.

| Item | Unit price | Full stack *estimate* | Paused *estimate* | Full stack *measured* | Paused *measured* |
|---|---|---|---|---|---|
| EKS control plane, standard support | $0.10/h (extended support adds $0.50/h: `upgrade_policy = STANDARD` prevents it) | $0.100 | $0.100 | | |
| Nodes: 2 × Graviton spot, 16 GiB each | spot $/h: r7g.large 0.051 · r6g.large 0.059 · m6g.xlarge 0.068 · m7g.xlarge 0.091 · c7g.2xlarge 0.165 (on-demand 0.12–0.33) | $0.10–0.18 | $0 | | |
| Public IPv4 | $0.005/h per address: 2 nodes + NLB in 2 AZs | $0.020 | $0.010 | | |
| EBS gp3 | $0.096/GB-month: 2 × 50 GB node roots + ~100 GB PVCs | $0.026 | $0.013 (PVCs only) | | |
| NLB (Envoy Gateway) | $0.0252/h + $0.006 per NLCU-hour (demo traffic is far below 1 NLCU) | $0.026 | $0.026 | | |
| CloudWatch Logs: EKS audit + authenticator | $0.70/GB ingested + $0.03/GB-month stored | $0.03–0.10 (50–150 MB/h; measure) | ≈ $0.01 | | |
| S3, Glue, SSM, Lambda, SNS, Scheduler, Budgets | ≈ $0 at this scale (2 budgets with actions are free) | ≈ $0 | ≈ $0 | | |
| **Total** | | **$0.30–0.45/h** | **≈ $0.16/h** | | |

What this means:

- A 4-hour session costs **about $1.2–1.8**. Phase 6's ~20 cloud hours fit the $7–10 estimate below.
- A paused cluster is not free: about $0.16/h, or **≈ $3.8 per day**. Pause only for short breaks; run
  `cloud-down` overnight.
- The biggest uncertain line is the **EKS audit log** volume. If the measured ingestion is above ~$0.05/h, keep
  only `authenticator` (`enabled_cluster_log_types`) outside security tests.
- **Spot interruption rate in ap-southeast-1** (Spot Instance Advisor): c7g.2xlarge < 5% · r6g.large 5–10% ·
  r7g.large 10–15% · m6g.xlarge and m7g.xlarge > 20%. Put types with fewer interruptions first when the node list
  is finalized (ADR 0505); keep at least two types for capacity.
- OpenCost shows the split between namespaces, but prices spot nodes at on-demand rates. Use it for relative
  shares and Cost Explorer for absolute numbers.

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
