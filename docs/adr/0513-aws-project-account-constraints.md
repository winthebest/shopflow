# 0513. Phase 6 on an AWS-managed project account: Sydney, on-demand, no GitHub OIDC

- Status: Accepted
- Date: 2026-10-18
- Lane: sf-cloud
- Supersedes: 0505 (Graviton spot nodes), 0509 (CI cannot apply; OIDC roles scoped to one workflow)
- Amends: 0501 (the two reapers), 0504 (the spot wording), 0500 and 0502 (the reaper that holds the state lock)

## Context

The account the user opened is an AWS-managed **project account** of the new AWS experience: project "Blank
Canvas", Paid plan, AWS Builder ID and `aws login` sign-in, no root user, an AWS-managed organization. The user
declined "activate advanced features": it is irreversible, and it is unclear whether the credits survive it. So
the AWS-managed SCPs stay
([managed policies for projects](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html)):

- **RegionFloor** denies every action outside the project Region (`ap-southeast-2`), `us-east-1` and `us-west-2`.
- **Paid Plan for projects** denies:
  - `ec2:RequestSpotInstances`, `ec2:RunInstances` with `ec2:InstanceMarketType = spot`, `ec2:CreateFleet`;
  - `iam:*Provider*` (no OIDC or SAML identity providers), `iam:*LoginProfile*`, `iam:*Alias*`, IAM groups;
  - `sso:CreateInstance` (no IAM Identity Center).
- **ProtectManagedRoles** protects `role/managed/*`, the roles AWS manages, including the admin `aws login` signs
  in to.
- **Spend limit** SCPs, applied near the user's monthly spend limit, deny `lambda:InvokeFunction`,
  `scheduler:CreateSchedule`, `eks:CreateCluster/CreateNodegroup`, `ec2:RunInstances/CreateVolume` and more
  (`docs/cost.md`).

A read-only probe on 2026-10-16 (profile `shopflow-probe`, the admin role `role/managed/AccountFullAccessRole`)
confirmed:

- reads in Sydney work;
- `ap-southeast-1` is an explicit deny;
- a spot dry-run is denied, and an on-demand dry-run would succeed;
- `iam:CreateOpenIDConnectProvider` is denied;
- every candidate Graviton type is offered in all three Sydney AZs;
- the on-demand vCPU quota is 16.

## Decision

- **Region `ap-southeast-2`, AZ `ap-southeast-2a`** (zone ID `apse2-az1`). All three AZs offer every node type, so
  the first one wins; the single-AZ rule of 0504 is unchanged.
- **On-demand Graviton nodes:** `r6g.large`, `r7g.large`, `r8g.large`, 2 vCPU / 16 GiB each, in that priority
  order (cheapest first). The managed node group launches the first type with capacity. The node group is named
  `nodes`.
  - Sydney on-demand: $0.1208 / $0.1284 / $0.1412 per hour.
  - The 4 vCPU types cost 50–60% more for the same memory (m6g.xlarge $0.192); the full stack may need a third
    node, measured first.
- **No GitHub → AWS access:**
  - the OIDC provider, the `ci-plan` and `reaper` roles, the GitHub reaper workflow and the infra-ci `plan` job
    are removed;
  - `tofu plan` runs from the operator's laptop;
  - CI stays offline (fmt, validate, `tofu test` with mocked providers, tflint, trivy, ShellCheck, Python tests).
- **Two in-AWS kill switches, one Lambda:**
  - an **hourly EventBridge sweep** in layer 0, always on; cloud-up refuses to start without it;
  - the **per-session schedule** from lease + 1 h, as before.

  The Lambda re-reads the lease and does nothing while it is valid. Its reserved concurrency of 1 serializes the two
  schedules. `scripts/cloud-reap.sh` stays as the laptop tool for a full layer-2 destroy under the state lock.
- **Third hard stop: the project spend limit** ($20/month, the user's setting in AWS Settings). It is gross before
  credits and pre-tax; it pauses the whole project at the limit and deletes the data after 90 days unresolved.
  Monthly budget thresholds under it are proposed in `docs/cost.md`; the user decides.
- **Identity:**
  - admin = an `aws login` session (role `role/managed/AccountFullAccessRole`), used only for layer 0;
  - operator = `shopflow-operator`, assumed from that session (role chaining, 1 h);
  - `operator_principal_arns` defaults to the admin role;
  - no Identity Center, permission sets or IAM users.

## Alternatives considered

| Option | Why not |
|---|---|
| Activate advanced features (lifts the plan SCPs, allows a normal organization) | irreversible; unclear what happens to the credits; the user declined |
| A separate standard AWS account | loses the $200 credit and the spend limit |
| Spot through the node group anyway (Auto Scaling launches with its service-linked role, which SCPs do not affect) | relies on a gap in the policy's intent; untestable before a paid session; the user chose on-demand |
| GitHub reaper with IAM user keys in repository secrets | long-lived keys (ADR 0512 forbids them), and IAM users are a weaker identity than OIDC |
| One in-AWS reaper only | if cloud-up dies before arming its schedule, nothing reaps the session; the sweep covers that |

## Consequences

- Positive:
  - every resource layer 0–2 creates is allowed by the managed SCPs (see the check table in the PR and in
    `docs/runbooks/cloud-session.md`);
  - no long-lived credentials anywhere;
  - the kill switches no longer depend on GitHub.
- Negative:
  - nodes cost about twice as much as spot ($0.45–0.51/h for the stack vs $0.30–0.45);
  - no automatic `tofu plan` on pull requests;
  - **near the spend limit the in-AWS reaper cannot be invoked** (DenyLambdaUsage), so the laptop `cloud-down` is the
    guaranteed path;
  - in an AWS-managed organization, cost allocation tags may only be activatable from the management account.
- When to revisit:
  - the user activates advanced features or moves to a standard account (spot and OIDC come back: reopen 0505 and
    0509);
  - the measured CPU of the full stack needs a different node shape.
