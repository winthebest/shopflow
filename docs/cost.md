# Cost: AWS account, budget and measured $/hour

Owner: sf-cloud. Numbers marked *estimate* come from AWS public prices; numbers marked *measured* come from Cost
Explorer with the **Credit** charge type excluded. Fill the measured columns after each session.

## Account and deadlines

The account is an AWS-managed **project account** of the new AWS experience (ADR 0513): sign-in with an AWS
Builder ID and `aws login`, no root user, an AWS-managed organization, and AWS-managed SCPs.

| Item | Value |
|---|---|
| Plan | **Paid plan** since 2026-10-16 (card on file) |
| Credits | $200, valid until about 12 months after sign-up (~2027-10); spend beyond the credits is charged to the card |
| Project cap | total spend before credits ≤ **$100** (the cumulative budget below; the user's cap, unchanged by the larger credit) |
| Spend limit | **$20 per month**, set by the user in AWS Settings (an AWS-managed control, see below). Not managed by this repo |
| Region | `ap-southeast-2` (Sydney): the project Region; the SCPs allow only it, `us-east-1` and `us-west-2` |

## Guardrails

Layer 0 (`infra/tofu/modules/cost-guardrails`, `infra/tofu/modules/reaper-lambda`) plus the account's spend limit.

AWS applies credits before Budgets evaluates spend, so a budget that counts credits stays at $0 and never alerts:
every budget here excludes credits.

| Guardrail | Setting |
|---|---|
| Budget `shopflow-total` | cost budget, **custom window 2026-10-01 → 2027-10-01** (one cumulative period: thresholds never reset at a month or year boundary), `include_credit = false`, `include_refund = false`, limit $100 |
| Emails | actual > $10, actual > $20, forecast > $25 (cumulative) |
| Budget Action | actual > $25 (cumulative) → attaches `shopflow-budget-deny` (no new EKS cluster/node group, EC2, EBS, ELB, NAT, EIP) to `shopflow-operator` and the LB controller role. Teardown, scale-down and reads keep working. Deliberate checkpoint: review this file, then raise the threshold (`action_threshold_usd`) and reset the action |
| Cost Anomaly Detection | services monitor (AWS's default one is reused when present), each anomaly ≥ $1 sent immediately to SNS `shopflow-alerts` |
| Cost allocation tags | `project`, `env`. In an organization only the management account can activate them, and this account is a member of an AWS-managed organization: expect `activate_cost_allocation_tags` to fail; verify at Approval 1 |
| Lease + kill switches | the real stop: Budgets refresh only every 8–12 h. Lease default 4 h (max 8 h). Two in-AWS reapers run the same Lambda: the **hourly sweep** (always on, layer 0) and the **per-session schedule** from lease + 1 h (cloud-up). There is no GitHub reaper (ADR 0513) |
| Spend limit (AWS, user setting) | $20/month, gross before credits and pre-tax; pauses the whole project at the limit (below) |

Tags reach controller-created resources too: launch template `tag_specifications` (instances, volumes, ENIs),
EBS CSI `extraVolumeTags`, vpc-cni `ADDITIONAL_ENI_TAGS`, LB controller `--default-tags`.

### The spend limit and how it meets the budgets

What the AWS documentation says:

- **What it counts.** It is "the most you'll pay per month for a project", and "Spend limits exclude credits and
  apply to pre-tax charges". So it counts gross usage, like our budgets, per calendar month
  ([create a spend limit](https://docs.aws.amazon.com/accounts/latest/reference/create-spend-limit.html)).
- **Notifications** come at 50%, 75% and 90% of actual cost, or when spend is on track to exceed the limit within
  10 days (same page).
- **Near the limit**, depending on the early cost controls the user enabled
  ([early controls](https://docs.aws.amazon.com/accounts/latest/reference/create-spend-limit.html#spend-limit-early-controls),
  [SCPs for spend limits](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html#scps-for-spend-limits)):
  - about 7 days before the forecast reaches the limit, an AWS-managed SCP stops new resources;
  - about 5 days before, idle EC2 instances are paused;
  - about 4 days before, the top cost drivers among EC2, RDS, Lambda, Bedrock and SageMaker are paused.
- **What those SCPs deny for us:** `lambda:InvokeFunction` (DenyLambdaUsage), `scheduler:CreateSchedule`,
  `eks:CreateCluster`, `eks:CreateNodegroup`, `ec2:RunInstances`, `ec2:CreateVolume`,
  `elasticloadbalancing:CreateLoadBalancer` and `autoscaling:CreateAutoScalingGroup`. "Near" is a forecast ("about
  N days before you would reach your limit"), not a stated percentage.
- **At the limit**, "AWS pauses your project and stops all resources. Your data is preserved." Raising the limit
  reactivates the project. "If you take no action within 90 days of your project being paused, AWS permanently
  deletes your project data" (same page).
- **What a pause does to the EKS control plane, EBS volumes and the NLB, and whether they keep billing:** the
  pages do not say. Unknown.

What it means for this project (failure mode in
[`runbooks/cloud-session.md`](runbooks/cloud-session.md#when-the-spend-limit-is-near)):

- **The in-AWS reaper stops working exactly when the limit is near.** The Scheduler role is not a service-linked
  role, so DenyLambdaUsage applies to its invoke. cloud-up's per-session schedule cannot be created either. The
  guaranteed path is `make cloud-down` from the laptop: no SCP above denies `Delete*`, `ssm:PutParameter` or
  `s3:PutObject`.
- **At $0.45–0.51/h**, $20 is about 40 cluster-hours in a month, minus layer 0 and paused hours. The lease (4 h)
  and the sweep keep a forgotten session well below that.
- **The cumulative thresholds sit above the monthly limit.** The emails at $10/$20 and the action at $25 fire after
  the monthly limit in the first month, and the cumulative $25 action then fires only $5 into the second month.

### Proposal: monthly thresholds under the $20 spend limit

The user decides; layer 0 would change only after that. The spend limit itself stays the user's setting.

Every alarm and deny should fire before the monthly limit, allowing for Budgets' 8–12 h refresh. At $0.5/h, that
lag is $4–6 of spend.

| Guardrail | Proposed setting | Why |
|---|---|---|
| New budget `shopflow-monthly` | MONTHLY cost budget, `include_credit = false`, limit $20 (mirrors the spend limit) | thresholds that reset with the spend limit |
| Email | actual ≥ 40% ($8) | early warning, a day's work left |
| Email | forecast ≥ 75% ($15) | the month is heading for the limit |
| Budget Action (moved here from the cumulative budget) | actual ≥ 60% ($12) → `shopflow-budget-deny` | $12 plus up to $6 of Budgets lag stays under $20; a deliberate checkpoint as today |
| `shopflow-total` (cumulative $100) | keep the emails at $10/$20/forecast $25; drop its action (the monthly one replaces it) | keeps the project cap; avoids the cumulative action firing early in month two |

## SSM parameter types

- Control values (`/shopflow/aws/control/*`: lease, backup pointer, initdb marker, session, resume size; and
  `/shopflow/aws/kafka/cdc-epoch`) are **String**: they are not secret, and the reapers can read the lease
  without `kms:Decrypt`.
- **Every secret is SecureString** (default `aws/ssm` key), written only by `scripts/aws-seed-params.sh` from stdin.
  Never put a secret in a String parameter. See ADR 0508.

## $/hour

*Estimates* come from AWS public prices for `ap-southeast-2`.
- **EC2 and EBS:** the EC2 offer file, publication date 2026-10-15.
- **EKS, ELB, VPC and CloudWatch:** their price list files, read 2026-10-18.
- The **on-demand node prices** were also read with the Price List API (2026-10-16).

Nodes are on-demand: the project account denies spot (ADR 0513). Replace the estimates with Cost Explorer numbers
(Credit charge type excluded) after the first sessions.

| Item | Unit price | Full stack *estimate* | Paused *estimate* | Full stack *measured* | Paused *measured* |
|---|---|---|---|---|---|
| EKS control plane, standard support | $0.10/h (extended support adds $0.50/h: `upgrade_policy = STANDARD` prevents it) | $0.100 | $0.100 | | |
| Nodes: 2 × Graviton on-demand, 16 GiB each (types in `cloud-contract.json`, ADR 0513) | r6g.large $0.1208/h (first choice) · r7g.large $0.1284 · r8g.large $0.1412 | $0.242 (r6g); $0.257 (r7g); $0.282 (r8g) | $0 | | |
| Public IPv4 | $0.005/h per address: 2 nodes + NLB in 2 AZs | $0.020 | $0.010 | | |
| EBS gp3 | $0.096/GB-month: 2 × 50 GB node roots + ~100 GB PVCs | $0.026 | $0.013 (PVCs only) | | |
| NLB (Envoy Gateway) | $0.0252/h + $0.006 per NLCU-hour (demo traffic is far below 1 NLCU) | $0.026 | $0.026 | | |
| CloudWatch Logs: EKS audit + authenticator | $0.67/GB ingested + $0.033/GB-month stored | $0.03–0.10 (50–150 MB/h; measure) | ≈ $0.01 | | |
| S3, Glue, SSM, Lambda, SNS, Scheduler, Budgets | ≈ $0 at this scale (the hourly sweep is ~730 Lambda invocations a month) | ≈ $0 | ≈ $0 | | |
| **Total** | | **$0.45–0.51/h** (r6g; +$0.04 on r8g) | **≈ $0.16/h** | | |

What this means:

- A 4-hour session costs **about $1.8–2.1**, against $1.2–1.8 with spot in Singapore.
- A paused cluster is not free: about $0.16/h, or **≈ $3.8 per day**. Pause only for short breaks; run
  `cloud-down` overnight.
- The biggest uncertain line is the **EKS audit log** volume. If the measured ingestion is above ~$0.05/h, keep
  only `authenticator` (`enabled_cluster_log_types`) outside security tests.
- **Sizing:** r6g.large has 2 vCPU, so two nodes give 4 vCPU / 32 GiB, enough for `core,obs-lite`. The full stack
  (data, rt, batch) needs a third node (`node_desired_size`, quota: 16 on-demand vCPU in this account) or xlarge
  types. Measure it before any change.
- OpenCost prices nodes at on-demand rates, which is now right. Use it for the split between namespaces, and Cost
  Explorer for absolute numbers.

## Budget plan (*estimate*, Sydney on-demand; update with measured $/hour)

| Item | Cloud hours | Estimate |
|---|---|---|
| Layer 0 for up to 12 months | — | $3–8 |
| Phase 6 (integration, measurements, teardown/reaper tests) | ~20 h | $9–11 |
| Phase 7 (EKS game days, restore drills) | ~15 h | $7–8 |
| Phase 8 (Kyverno Enforce, video, interview demo) | ~8 h | $4 |
| Reserve (forgotten teardown, orphaned LB, price drift) | — | ≥ $69 |

With the $20 monthly spend limit, plan at most ~35 cluster-hours a month; Phase 6 and 7 fit in two months.

## Session log

| Date | Session | Hours | Profiles | Cost before credits | Notes |
|---|---|---|---|---|---|
| | | | | | |
