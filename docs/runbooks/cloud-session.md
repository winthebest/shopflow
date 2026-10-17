# Runbook: AWS cloud session

Owner: sf-cloud. Every AWS action below happens only after the user approves it through the orchestrator.
Constants (names, paths, regions, the Pod Identity table) live in [`infra/cloud-contract.json`](../../infra/cloud-contract.json),
read by OpenTofu and by the scripts; this page explains them, it does not redefine them.

The account is an AWS-managed **project account** (new AWS experience, Paid plan): Region `ap-southeast-2`,
no spot, no GitHub OIDC, no IAM Identity Center. Its SCPs and what they mean for each resource are in
[SCP check](#scp-check); the decision is ADR 0513.

| Layer | Directory | Lifetime | Applied by |
|---|---|---|---|
| 0 bootstrap | `infra/tofu/bootstrap` | whole project | **admin** `aws login` session, profile `shopflow-admin` (it creates IAM) |
| 1 network | `infra/tofu/network` | whole project (free) | `shopflow` operator profile |
| 2 cluster | `infra/tofu/cluster` | one session | `make cloud-up` / `make cloud-down` |

## First AWS session: the order

Each **approval** below is the user's, relayed by the orchestrator. Costs are estimates from [`docs/cost.md`](../cost.md).

| # | Step | Who | Time | Cost | Details |
|---|---|---|---|---|---|
| 1 | MFA in AWS Settings; CLI profiles `shopflow-admin` (`aws login`) and `shopflow` (operator); `aws login` | user | ~15 min | $0 | [One-time account setup](#one-time-account-setup-user) |
| 2 | **Approval 1.** Layer 0 with the admin session: local state → apply → migrate the state to S3. Confirm the SNS email. If the anomaly monitor already exists, reuse it. Run the "verify at Approval 1" checks | sf-cloud + user | ~15 min | ≈ $0.5/month from now on | [Bootstrap layer 0](#bootstrap-layer-0-first-time) |
| 3 | **Approval 2.** Layer 1 with the operator profile | sf-cloud | ~3 min | $0 | same section |
| 4 | Seed the secrets through pipes, never a file: `scripts/platform-secrets.sh --aws-json \| make cloud-seed-params` and `scripts/data-secrets.sh --aws-json \| make cloud-seed-params`; values only you know (the Discord webhook, the Grafana login you choose) with the silent prompt | user | 10 min | $0 | [Bootstrap layer 0](#bootstrap-layer-0-first-time), end of section |
| 5 | Check the guards: the hourly sweep is `ENABLED` (`aws scheduler get-schedule --group-name shopflow --name shopflow-reaper-sweep`), the Budget Action is in standby | sf-cloud | 5 min | $0 | [Kill switches](#kill-switches) |
| 6 | `make cloud-up CLOUD_ARGS="--dry-run --profiles core,obs-lite"`: reads AWS, prints the plan, changes nothing | sf-cloud | 5 min | $0 | [Session lifecycle](#session-lifecycle) |
| 7 | **Approval 3.** `make cloud-up CLOUD_ARGS="--profiles core,obs-lite"` (on AWS, `core` also runs External Secrets and the LB controller; `obs-lite` adds OpenCost), then the first-session checks | sf-cloud | 30–45 min to RTO | ≈ $0.45–0.51/h | [First-session checks](#first-session-checks) |
| 8 | `make cloud-down` (same day); the orphan check must be clean | sf-cloud | ~20 min | — | |
| 9 | Next day: Cost Explorer with the Credit charge type excluded → measured $/hour in `docs/cost.md` | sf-cloud | 15 min | $0 | [`docs/cost.md`](../cost.md) |

Expected spend for the first session: about **$2 for 4 hours** (`core,obs-lite`, on-demand), plus layer 0 at
≈ $0.5/month.

Guardrails live from step 2:
- emails at $10 and $20 actual and $25 forecast (cumulative);
- the Budget Action at $25 blocks new capacity;
- the lease (4 h by default), with the hourly sweep and the per-session reaper, is the real stop;
- above them sits the user's **$20/month spend limit**: see
  [When the spend limit is near](#when-the-spend-limit-is-near), and the monthly thresholds proposed in
  `docs/cost.md`.

Prerequisites from other lanes, all in place: the root-app hook with the session parameters (ADR 0206), the
`profiles-aws` trees (`core`, `obs`, `obs-lite`), the `aws` overlays of their components (the Envoy Gateway Service
carries `loadBalancerClass: service.k8s.aws/nlb` and the operator CIDR), and the NetworkPolicies with their aws
variant (ADR 0208).

## One-time account setup (user)

Already done: Paid plan, card, the $20/month spend limit, the credits.

- [ ] **MFA** for your sign-in, in AWS Settings (Security). There is no root user on this account type.
- [ ] AWS CLI v2 recent enough for `aws login`, and two profiles in `~/.aws/config`:
  - `shopflow-admin`: the `aws login` session. It signs in to the account's admin role
    `role/managed/AccountFullAccessRole` and is used only for layer 0;
  - `shopflow`: the operator, assumed from it.

  `aws login --profile shopflow-admin` writes its own session settings into that profile. Add only the region,
  then the operator profile:

      ```ini
      [profile shopflow-admin]
      # written by: aws login --profile shopflow-admin
      region = ap-southeast-2

      [profile shopflow]
      role_arn = arn:aws:iam::<ACCOUNT_ID>:role/shopflow-operator
      source_profile = shopflow-admin
      region = ap-southeast-2
      ```

- [ ] `aws login --profile shopflow-admin`; once layer 0 exists, `aws sts get-caller-identity --profile shopflow`.
      Role chaining caps operator sessions at 1 h; the CLI refreshes them while the admin session is valid.
      Whether the CLI refreshes a role chained from an `aws login` session: verify at Approval 1.

There is no IAM Identity Center (the SCPs deny `sso:CreateInstance`), no permission set and no IAM user. The
operator role trusts the admin role (`operator_principal_arns`, layer 0).

## GitHub settings

None for AWS. No workflow reaches AWS: the account denies OIDC identity providers, so there is no OIDC `sub`
customization, no `AWS_ACCOUNT_ID` variable, no `ALERT_EMAIL` secret and no `INFRA_PLAN_ENABLED`. `tofu plan` runs
from the laptop (`AWS_PROFILE=shopflow tofu -chdir=infra/tofu/<layer> plan`).

## Bootstrap layer 0 (first time)

The state bucket is created by this layer, so the first apply runs on local state and then migrates.

```sh
aws login --profile shopflow-admin
export AWS_PROFILE=shopflow-admin TF_VAR_aws_account_id=<ACCOUNT_ID> TF_VAR_alert_email=<you@example.com>
cd infra/tofu/bootstrap
printf 'terraform {\n  backend "local" {}\n}\n' > local_override.tf      # gitignored
tofu init && tofu plan -out=l0.plan && tofu apply l0.plan
rm local_override.tf l0.plan
tofu init -migrate-state -backend-config="bucket=shopflow-tfstate-<ACCOUNT_ID>"
rm -f terraform.tfstate terraform.tfstate.backup                         # now in S3
```

- [ ] Confirm the SNS subscription email ("AWS Notification - Subscription Confirmation").
- [ ] If `apply` fails on the anomaly monitor ("limit exceeded"), the account already has AWS's default services
      monitor: `aws ce get-anomaly-monitors`, then re-apply with `TF_VAR_anomaly_monitor_arn=<arn>`.
- [ ] Cost allocation tags: in an organization only the management account can activate them, and this account
      is a member of an AWS-managed organization. Leave `activate_cost_allocation_tags = false`; check in Billing
      whether AWS Settings offers it (verify at Approval 1).

Layer 1, with the operator profile:

```sh
export AWS_PROFILE=shopflow TF_VAR_aws_account_id=<ACCOUNT_ID>
tofu -chdir=infra/tofu/network init -backend-config="bucket=shopflow-tfstate-<ACCOUNT_ID>"
tofu -chdir=infra/tofu/network apply
```

Secrets for External Secrets (SecureString). Values go from their producer to SSM through a pipe or a silent
prompt: never a command line, and never a file (backups and sync tools such as Time Machine or iCloud may copy it
before it is deleted).

```sh
# Generated values; each producer refuses to write to a terminal.
scripts/platform-secrets.sh --aws-json | make cloud-seed-params   # shop-db role passwords (sf-platform)
scripts/data-secrets.sh --aws-json | make cloud-seed-params       # Trino secrets, with their group (sf-data)
# Values only you know: asked without echo, one prompt per key.
make cloud-seed-params CLOUD_ARGS="--prompt observability/alertmanager-webhook --keys url"
# Grafana login (port-forward only): when you can, paste a password generated by your password manager.
make cloud-seed-params CLOUD_ARGS="--prompt observability/grafana-admin --keys admin-user,admin-password"
# Replace existing values (and whole groups): add --rotate.
scripts/data-secrets.sh --aws-json | make cloud-seed-params CLOUD_ARGS=--rotate
```

Values that must match each other (passwords and the bcrypt hashes of them in another Secret) are declared by the
producer as a group, e.g. `"_groups": [["lakehouse/trino-dbt", "lakehouse/trino-exporter",
"lakehouse/trino-metabase", "lakehouse/trino-password-db"]]`. A group is written whole or not at all: if SSM holds
only part of it and `--rotate` is not given, the script stops before writing any parameter. When a producer adds a
member to an existing group (e.g. a new Trino login), the next seed needs `--rotate`, which renews the whole group
together.

## Session lifecycle

| Command | What it does |
|---|---|
| `make cloud-up` | preflight (operator role, Budget Action not fired, EKS version in standard support, no active lease, backup chain) → provisional lease → apply layer 2 → kubeconfig → Argo CD (chart + values from `deploy/argocd/bootstrap/`; admin password from SSM, see below) → root apps through `scripts/platform-root-apps.sh --overlay aws` → wait Synced/Healthy → Postgres chain (after a recovery: the order/payment id sequences move past the restored ids, + 100000, or past the highest acked id + 1000 with `CLOUD_ARGS="--ack-log <k6 acks.jsonl>"`, see [restore.md](restore.md)) → CDC epoch → smoke (a checkout through the NLB is in `lake_ro` `bronze.orders` with this session's `_cdc_epoch` within 2 min, queried as the read-only `exporter` user; the password goes through stdin) → network policy probe (a pod in `shop` that no allow rule selects resolves shop-db but times out on Postgres and the internet; result in `out/cloud/<session>/netpol-probe.json`) → prints RTO-infra and RTO-service → final lease + per-session reaper schedule. Preflight also refuses to start unless the hourly reaper sweep is `ENABLED` |
| `make cloud-up CLOUD_ARGS=--dry-run` | reads AWS, runs `tofu plan`, prints every change instead of making it |
| `make cloud-up CLOUD_ARGS=--resume` | continues a cloud-up that stopped (reuses the recorded session plan) |
| `make cloud-up CLOUD_ARGS="--pitr 2026-11-02T10:15:00Z"` | restores Postgres to that time |
| `make cloud-extend HOURS=2` | lease + 2 h (max 8 h from now) and moves the Lambda schedule; nothing else |
| `make cloud-pause` / `make cloud-resume` | node group to 0 and back (< 4 h breaks; control plane, NLB and volumes still bill ~$0.16/h) |
| `make cloud-down` | auto-sync off → final backup + pointer → evidence to S3 → Gateway/LB deleted, wait for ELBv2 → stateful CRs + PVCs deleted, wait for EBS → uninstall Argo CD → `tofu destroy` layer 2 (3 tries) → orphan check in the account's regions (`scan_regions`) → drop lease, session, schedule. Re-run it after any interruption |
| `make cloud-down CLOUD_ARGS=--force-api` | paused or unreachable cluster: AWS API teardown like the reapers, no final backup |
| `make cloud-reap` | an expired session, from the laptop: takes the layer-2 state lock, re-reads the lease, API teardown, `tofu destroy` under the lock, orphan check |

### Argo CD admin on AWS

Never the chart's random initial secret (ADR 0205). The first cloud-up generates a password inside a pipe straight
into SSM `/shopflow/aws/argocd/admin-password` (SecureString); nobody types or sees it. Each cloud-up reads it,
pipes it through `htpasswd -i` and hands Helm only the bcrypt hash, through process substitution (no file, no
argv), as `make up` does locally. Access: `make cloud-argocd-ui` (port-forward, user `admin`) and
`make cloud-argocd-password` (copies it to the clipboard). To rotate it, delete the parameter; the next cloud-up
generates a new one.

### Parameters cloud-up passes to the root apps

The only differences between local and AWS are the `aws` overlays and these parameters
(`--param key=value` to `scripts/platform-root-apps.sh`, owned by sf-platform). Hooks receive the session
kubeconfig through `KUBECONFIG`; after the apps are healthy cloud-up runs `scripts/cdc-epoch.sh wait --epoch N`
(sf-data: control topic, `meta.cdc_epochs`, waits for `SnapshotCompleted`).

| Parameter | Value |
|---|---|
| `operatorCidr` | operator's public IP `/32` (NLB `loadBalancerSourceRanges`; also the EKS API allow-list) |
| `pg.recoveryFrom` | `serverName` of the backup chain to recover from; empty on the very first session (initdb) |
| `pg.serverName` | new `shop-db-<session>` every session, so a restored cluster never writes into the old chain |
| `pg.recoveryTargetTime` | `--pitr` value or empty (latest) |
| `cdcEpoch` | previous epoch + 1. Also written to SSM `/shopflow/aws/kafka/cdc-epoch` before the sync; ESO turns it into Secret `kafka/cdc-epoch` (key `epoch`), which the connector reads as `${secrets:kafka/cdc-epoch:epoch}` |
| `aws.region`, `aws.vpcId`, `aws.clusterName` | AWS Load Balancer Controller Helm values `region`, `vpcId` (required: pods cannot read IMDS, hop limit 1), `clusterName` |
| `aws.accountId` | External Secrets store chart value `accountId` (role ARNs `shopflow-eso-<namespace>`) |
| `aws.dataBucket` | `shopflow-data-<account>` (Iceberg warehouse, CNPG backups, Flink checkpoints) |

### Pod Identity: namespace/service account → role

Source of truth: `pod_identities` in `infra/cloud-contract.json` (layer 0 creates the roles, layer 2 the
associations). Each role's trust policy also pins the namespace and service account through the Pod Identity
session tags, so pointing another service account at a role does not work.

| Key | Namespace / service account | Access |
|---|---|---|
| `ebs-csi` | `kube-system/ebs-csi-controller-sa` | AmazonEBSCSIDriverPolicy |
| `aws-lb-controller` | `kube-system/aws-load-balancer-controller` | upstream controller policy (v3.5.0) |
| `cnpg` | `shop/shop-db` | `pg-backup/*` |
| `kafka-connect` | `kafka/cdc-connect` (KafkaConnect `cdc`) | `iceberg/*` + Glue `bronze`/`silver`/`gold` |
| `trino` | `lakehouse/trino` | `iceberg/*` + Glue; explicit Deny on `pg-backup/*` |
| `flink` | `lakehouse/flink` | `flink-ckpt/*` (+ Glue/`iceberg/*` only with `flink_writes_iceberg`) |
| `external-secrets` | `external-secrets/external-secrets` | may only assume `shopflow-eso-<namespace>` |

External Secrets: the external-secrets app (sf-cloud) holds the controller and one ClusterSecretStore `ssm-<namespace>`
per namespace in `eso_namespaces` (`deploy/platform/external-secrets/aws/secret-stores`). A store is usable only
from its namespace (`conditions`) and assumes `shopflow-eso-<namespace>`, which reads only `/shopflow/aws/<namespace>/*`.
Each `ExternalSecret` lives in its component's aws overlay and belongs to the component owner (gitops.md §1b). It
keeps the name and keys of the local SOPS Secret, so workloads do not change. There is one writer per Secret: a
Secret written by a Job, an operator or cert-manager never gets an ExternalSecret.

| Secret | SSM parameter | Owner (aws overlay) | Notes |
|---|---|---|---|
| `observability/grafana-admin` (`admin-user`, `admin-password`) | `/shopflow/aws/observability/grafana-admin` | sf-sre, kube-prometheus-stack | seeded with `--prompt` (the login you use) |
| `observability/alertmanager-webhook` (`url`) | `/shopflow/aws/observability/alertmanager-webhook` | sf-sre, kube-prometheus-stack | seeded with `--prompt` |
| `shop/shop-db-<role>` (basic-auth), one per login role enabled in `deploy/charts/shop-db` (`roles`); today `debezium`, `trino-pg`, `airflow`, `flink-serving`, `grafana-serving`, `fulfillment-worker` (users `debezium`, `trino_pg`, `airflow`, `flink_serving`, `grafana_serving`, `fulfillment_worker`) | `/shopflow/aws/shop/<name>` | sf-platform, shop-db chart | `scripts/platform-secrets.sh` reads the same list, so a new role is seeded without changing this runbook; role passwords must keep their first values (the roles come back with every restored database); label `cnpg.io/reload` |
| `lakehouse/trino-internal` (`shared-secret`) | `/shopflow/aws/lakehouse/trino-internal` | sf-data, trino | |
| `lakehouse/trino-dbt`, `lakehouse/trino-exporter`, `lakehouse/trino-metabase` (`username`, `password`), `lakehouse/trino-password-db` (`password.db`) | `/shopflow/aws/lakehouse/<name>` | sf-data, trino | one seed group: the bcrypt file must match every password. `trino-metabase` is seeded ahead of Metabase on AWS (no ExternalSecret reads it yet) |

Not ExternalSecrets, because another writer owns them: `kafka/cdc-epoch` (`scripts/cdc-epoch.sh`; cloud-up keeps
the last epoch in SSM `/shopflow/aws/kafka/cdc-epoch`), KafkaUser Secrets (Strimzi), `lakehouse/trino-tls`
(cert-manager), `lakehouse/trino-pg` (copy Job). `polaris-*`, `lake-s3-*`, `seaweedfs-*` and the `polaris` role
with `shop/shop-db-polaris` do not exist on AWS (Glue and Pod Identity instead). Generated values are seeded by
their owner's producer piped into `aws-seed-params.sh`; existing parameters are kept unless `--rotate`, so
re-running a producer that generates new random values never changes a seeded password.

`scripts/cloud-manifests-check.sh` (`make cloud-manifests`) checks every ExternalSecret in `deploy/`: it must use
the store of its namespace, read only `/shopflow/aws/<namespace>/*`, carry `SkipDryRunOnMissingResource=true` and
match the CRD of the pinned chart. One in a namespace without a store fails.

## Backup chain

SSM (String) holds the chain:

- `/shopflow/aws/control/pg-backup-pointer` = `{"serverName": "...", "backupId": "..."}`: the last completed
  backup; cloud-up moves it after the post-recovery backup, cloud-down after the final one.
- `/shopflow/aws/control/pg-initialized`: written once, after the very first initdb.

cloud-up decides, failing closed (ADR 0511):

| Pointer | Marker | Action |
|---|---|---|
| present, backup exists in S3 | any | recover from `serverName` (latest, or `--pitr`) into a new `serverName` |
| present, backup missing | any | **stop** |
| missing | missing | first session: initdb, then write the marker |
| missing | present | **stop**: never initdb over a database that existed |

When cloud-up stops: find the newest complete backup under `s3://shopflow-data-<account>/pg-backup/<serverName>/base/`
and write the pointer by hand (`aws ssm put-parameter --overwrite --type String ...`). Delete the marker only
after you have decided, in writing in the PR or session notes, that the old data may be lost.

RPO: graceful `cloud-down` archives the last WAL segment before its final backup (RPO ≈ 0). The reaper paths
have no final backup: RPO is bounded by the WAL `archive_timeout` (60 s; 28–56 s measured locally, see
[restore.md](restore.md)).

## Kill switches

| Switch | Where | When | How |
|---|---|---|---|
| Hourly sweep | layer 0: EventBridge Scheduler `shopflow/shopflow-reaper-sweep` → Lambda `shopflow-reaper` | every hour, always | re-reads the lease; while it is valid, nothing. Once it has expired or is missing: deletes NLBs → node groups → available EBS → the cluster through AWS APIs (only `project=shopflow` resources). With no session it is a silent no-op. cloud-up refuses to start when it is missing or disabled |
| Per-session reaper | Scheduler `shopflow/shopflow-reaper`, created by cloud-up | every 15 min from lease + 1 h | same Lambda; deletes its own schedule once nothing is left (never the sweep) |
| Laptop reaper | `make cloud-reap` (`scripts/cloud-reap.sh`, operator role) | by hand | takes the layer-2 state lock, re-reads the lease, API teardown, `tofu destroy -lock=false` under its lock, orphan check |

A missing or unreadable lease counts as expired. The Lambda runs one invocation at a time (reserved concurrency
1), so the two schedules never tear down concurrently. Failures alert by SNS email in two ways:
- the Lambda error alarm;
- the `shopflow-reaper-undelivered` alarm. Both schedules send an invocation they could not deliver to the SQS
  dead-letter queue `shopflow-reaper-dlq`: the spend-limit SCP denying `lambda:InvokeFunction`, or a broken
  scheduler role. Neither is a Lambda error.

The operator role may create, update or delete only the per-session schedule. The permissions boundary denies every
role any change to the sweep: only the layer-0 admin can touch it.
There is no GitHub reaper: the project account denies OIDC identity providers (ADR 0513).

Tests to run in the first sessions:
- a 10-minute lease → the sweep destroys the cluster within the hour;
- the per-session schedule deleted by hand → the sweep still destroys it;
- a paused cluster → still destroyed.

### When the spend limit is near

The user's spend limit ($20/month, gross before credits) is AWS's last stop, and it acts on the whole project
([create a spend limit](https://docs.aws.amazon.com/accounts/latest/reference/create-spend-limit.html),
[SCPs for spend limits](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html#scps-for-spend-limits)):

- **Days before the forecast reaches the limit** (with the early cost controls the user enabled), AWS-managed SCPs
  deny new resources: `lambda:InvokeFunction`, `scheduler:CreateSchedule`, `eks:CreateCluster/CreateNodegroup`,
  `ec2:RunInstances/CreateVolume`, `elasticloadbalancing:CreateLoadBalancer`. Idle or top-cost EC2 and Lambda may
  be paused.
  - **The in-AWS reapers stop working then:** Scheduler cannot invoke the Lambda.
  - cloud-up cannot create a cluster or its schedule.
- **At the limit**, AWS pauses the project and stops all resources; the data is kept. **After 90 days unresolved,
  AWS permanently deletes the project data:** the state bucket, the Postgres backups and the evidence.

What to do:

1. **`make cloud-down` from the laptop is the guaranteed path.** None of those SCPs deny `Delete*`, `ssm:PutParameter`
   or `s3:PutObject`, so the final backup, the evidence export and the destroy still work. Run it at the first
   spend-limit email (50%), not at the limit.
2. The lease sweep should have acted long before: the lease is 4 h, so a forgotten session costs ~$2, not $20.
   If a session is still running near the limit, the sweep has failed. Check the Lambda error alarm and the
   `shopflow-reaper-undelivered` alarm (an invocation denied by the spend-limit SCP lands in the dead-letter queue).
3. **What a pause does to billing** for the EKS control plane, EBS volumes and the NLB: the docs do not say.
   Assume EBS keeps billing (the data is kept). That is one more reason to have run `cloud-down` before.
4. **If the project is paused:** within 90 days, raise the spend limit in AWS Settings → Billing to reactivate it,
   then:
   - run `make cloud-down` (or `CLOUD_ARGS=--force-api`) and the orphan check;
   - copy anything that must survive out of AWS: `make cloud-evidence` output (already local under `out/`), and,
     if the project will be dropped, the layer-0 state and the last Postgres backup, with
     `aws s3 sync s3://shopflow-tfstate-<account>/ …` and `s3://shopflow-data-<account>/pg-backup/<serverName>/ …`.

## SCP check

Every resource layers 0–2 and a session create, against the AWS-managed SCPs
([managed policies for projects](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html)).

- **Allow-list:** the "Paid Plan for projects" SCP is a `Deny` with `NotAction` (an allow-list). Every service
  below is in it.
- **Probe:** a read-only probe on 2026-10-16 with the admin role.
- **Verify at Approval 1:** what only a real apply can show.

| Resource / action | Layer | Verdict | Source |
|---|---|---|---|
| S3 buckets (state, data): versioning, lifecycle, encryption, conditional writes | 0 | allowed | allow-list `s3:*`; RegionFloor allows Sydney |
| IAM roles (operator, EKS cluster/node, pod, ESO, Lambda, Scheduler, budget action), policies, permissions boundary | 0 | allowed | allow-list `iam:*`; DenyIAMRestrictedActions covers only providers, login profiles, aliases, groups and certificates; ProtectManagedRoles covers only `role/managed/*` |
| IAM OIDC provider for GitHub | 0 | **denied**, removed | DenyIAMRestrictedActions `iam:*Provider*`; probe: explicit deny |
| Operator trust on `role/managed/AccountFullAccessRole` | 0 | allowed (the trust policy lives on our role; the managed role is not modified) | ProtectManagedRoles only blocks changes to `role/managed/*`; probe: role path `/managed/` |
| `iam:CreateServiceLinkedRole` (EKS, node group, Auto Scaling, ELB) | 0/2 | allowed | allow-list `iam:*` |
| Budget + Budget Action (attaches the deny policy) | 0 | allowed | allow-list `budgets:*`; global endpoint in `us-east-1`, allowed by RegionFloor |
| Cost Anomaly Detection monitor + subscription | 0 | API allowed; whether a member account may own a monitor: **verify at Approval 1** | allow-list `ce:*` |
| Cost allocation tag activation | 0 | **likely fails** (management account only in an organization); not applied by default | AWS Billing docs; verify at Approval 1 |
| SNS topic + email subscription | 0 | allowed | allow-list `sns:*` |
| Lambda (python3.13, arm64), reserved concurrency 1 | 0 | allowed (concurrency quota 400: probe) | allow-list `lambda:*`; **near the spend limit `lambda:InvokeFunction` is denied** |
| Scheduler group, hourly sweep, per-session schedule | 0/session | allowed; near the spend limit `scheduler:CreateSchedule` is denied | allow-list `scheduler:*` |
| SQS dead-letter queue for both schedules (SSE-SQS) + its alarm | 0 | allowed; near the spend limit only `sqs:CreateQueue` is denied, so delivery to the existing queue still works | allow-list `sqs:*`, `cloudwatch:*` |
| CloudWatch alarm, log groups (EKS audit, Lambda) | 0/2 | allowed | allow-list `cloudwatch:*`, `logs:*` |
| SSM parameters (String, SecureString with `aws/ssm`) | 0/session | allowed | allow-list `ssm:*`, `kms:*` |
| Glue databases | 0 | allowed | allow-list `glue:*` |
| VPC, public subnets, route table, internet gateway, default SG (no NAT) | 1 | allowed | allow-list `ec2:*` |
| EKS cluster, access entries, add-ons (vpc-cni, kube-proxy, pod-identity-agent, coredns, EBS CSI, metrics-server), Pod Identity associations | 2 | allowed; near the spend limit `eks:CreateCluster/CreateNodegroup` are denied | allow-list `eks:*`, `eks-auth:*`; probe: EKS reads in Sydney |
| Managed node group, launch template, **on-demand** Graviton | 2 | allowed (probe: an on-demand dry-run would succeed). Auto Scaling launches with its service-linked role, which SCPs do not affect | allow-list `ec2:*`, `autoscaling:*` |
| Spot instances / spot node group | — | **denied**, not used | DenyRequestSpotInstances, DenySpotMarketType; probe: explicit deny |
| EC2 Fleet | — | **denied**, not used directly | DenyEC2Fleet |
| EBS gp3 volumes (root, EBS CSI through Pod Identity) | 2/session | allowed; near the spend limit `ec2:CreateVolume` is denied | allow-list `ec2:*`; DenyEC2InstanceStoreRootDevice does not apply (EBS root) |
| NLB, target groups, security groups (LB controller through Pod Identity) | session | allowed; near the spend limit `CreateLoadBalancer` is denied | allow-list `elasticloadbalancing:*` |
| Public IPv4 on nodes and the NLB | session | allowed | allow-list `ec2:*` |
| ECR pulls (add-on and controller images) | session | allowed | allow-list `ecr:*`, `ecr-public:*` |
| Price List API | ops | allowed (probe) | allow-list `pricing:*`; endpoint in `us-east-1` |
| Anything in another region (e.g. `ap-southeast-1`) | — | **denied** | RegionFloor; probe: explicit deny |
| IAM Identity Center | — | **denied**, not used | DenySSOCreateInstance |

Verify at Approval 1 (needs a valid `aws login` session; the probe session has expired):
- the operator role assumed from the admin session;
- the Cost Anomaly monitor in a member account;
- cost allocation tags;
- the `metrics-server` add-on offered in Sydney (`aws eks describe-addon-versions --addon-name metrics-server`).

## First-session checks

- [ ] `aws eks describe-cluster-versions`: bump `kubernetes_version` in the contract if 1.35 is not the newest
      standard-support version.
- [ ] `aws eks describe-addon-configuration --addon-name vpc-cni` / `aws-ebs-csi-driver`: confirm
      `enableNetworkPolicy`, `env.ADDITIONAL_ENI_TAGS` and `controller.extraVolumeTags` are in the schema.
- [ ] On-demand capacity in `ap-southeast-2a` for the node types (all three AZs offer them); if the node group stays
      `CREATE_FAILED`/pending, check the 16-vCPU on-demand quota and the order of `node_instance_types`.
- [ ] The Envoy Gateway Service (sf-platform aws overlay) sets `loadBalancerClass: service.k8s.aws/nlb` itself.
      Otherwise, if it is created before the LB controller's webhook is up, EKS's legacy cloud provider builds a
      Classic ELB without the `project` tag, which the reapers cannot delete.
- [ ] cloud-up step 8 passed: `netpol-probe.json` is `{"dns": "ok", "postgres": "timeout", "internet": "timeout"}`.
      A refused or open connection means EKS accepted the NetworkPolicies (ADR 0208) without enforcing them:
      check that the `aws-node` pods run the `aws-network-policy-agent` container (vpc-cni `enableNetworkPolicy`,
      layer 2).
- [ ] HPAs have a metrics source (EKS add-on `metrics-server`, layer 2): `kubectl get --raw
      /apis/metrics.k8s.io/v1beta1/nodes` returns node usage, and `kubectl -n shop get hpa` shows CPU targets with a value
      (average CPU per pod, e.g. `120m/400m`), not `<unknown>/400m`.
- [ ] `kubectl get clustersecretstores,externalsecrets -A`: every store `Valid`, every ExternalSecret
      `SecretSynced`. The usual causes are a missing `aws.accountId` parameter or an unseeded parameter.
- [ ] API server egress address (ADR 0208, `components/apiserver-aws` allows TCP 443 to the VPC CIDR and to the
      `kubernetes` Service IP `172.20.0.1`): keep only the one the network policy agent matches.
      1. `kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o
         jsonpath='{.items[*].endpoints[*].addresses[*]}'`: the control-plane ENIs, expected inside `10.60.0.0/16`;
         `kubectl get svc kubernetes`: ClusterIP `172.20.0.1` (EKS picks `172.20.0.0/16` for a VPC in `10.0.0.0/8`).
      2. In a scratch namespace `netpol-lab`, apply `default-deny` + DNS + one egress rule (TCP 443 to the VPC CIDR
         **only**), and from a pod there connect to `kubernetes.default.svc:443`. Repeat with `172.20.0.1/32`
         **only**. Delete the namespace.
      3. Send the result (which address connected) to sf-platform, who own the component. If both connect, the
         agent matches after the Service DNAT, and the VPC CIDR alone is enough.
- [ ] OpenCost (`kubectl -n opencost port-forward svc/opencost 9090`) shows cost per namespace (on-demand prices,
      which now match the nodes); compare its total with Cost Explorer's the next day.
- [ ] Node sizing (2 × r6g.large = 4 vCPU / 32 GiB, ADR 0513): `kubectl top nodes` and the pending pods under
      `core,obs-lite` at rest and during the smoke. Write the CPU and memory headroom in `docs/cost.md`. Before any
      session with `data`, `rt` or `batch`, decide between a third node (`node_desired_size`, 16-vCPU quota) and
      xlarge types from those numbers.
- [ ] `out/cloud/<session>/timings.json`: RTO-infra and RTO-service of the first session, for `docs/cost.md` and
      the Phase 6 report.
- [ ] Measure $/hour (full stack and paused) and fill [`docs/cost.md`](../cost.md).

## Troubleshooting

| Symptom | Action |
|---|---|
| cloud-down step 4/5 over 10 minutes (finalizers, LB) | re-run `make cloud-down`; still stuck → `CLOUD_ARGS=--force-api`; write a postmortem |
| `tofu destroy` keeps failing | `make cloud-down` again (it resumes); check `aws eks list-nodegroups`, ENIs in use, then the orphan check |
| Orphan check exit 2 | an untagged resource exists somewhere: identify it by hand; the scripts never delete it |
| Orphan check: `UNTAGGED … load-balancer` | a load balancer in the shopflow VPC without `project=shopflow`, typically a Classic ELB made by EKS's legacy cloud provider for a Service without the NLB class. Reapers cannot delete it (their IAM needs the tag). Check its tags and listeners, delete it (`aws elb delete-load-balancer` / `aws elbv2 delete-load-balancer`), and fix the Service that caused it. cloud-down stays failed until it is gone |
| Orphan check exit 3 | shopflow-tagged leftovers outside the home region or an EKS cluster: `cloud-down --force-api`, or delete by hand |
| `make cloud-reap`: "state lock is old" | a run died holding `cluster/terraform.tfstate.tflock`; confirm nothing runs, delete the object |
| cloud-up: "hourly reaper sweep is missing" / "is DISABLED" | layer 0 not applied, or the schedule was disabled: apply layer 0 or re-enable `shopflow/shopflow-reaper-sweep` before any session |
| Spend-limit email (50%/75%/90%) or an access denied on `lambda:InvokeFunction` / `eks:Create*` | the spend limit is near: `make cloud-down` now, see [When the spend limit is near](#when-the-spend-limit-is-near) |
| cloud-up: "Budget Action has fired" | spend passed $25: review `docs/cost.md`, raise `action_threshold_usd` in layer 0, then reset the action in the Budgets console |
| cloud-up: EKS version not in standard support | bump `kubernetes_version` in `infra/cloud-contract.json` |
