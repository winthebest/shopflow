# Runbook: AWS cloud session

Owner: sf-cloud. Every AWS action below happens only after the user approves it through the orchestrator.
Constants (names, paths, regions, the Pod Identity table) live in [`infra/cloud-contract.json`](../../infra/cloud-contract.json),
read by OpenTofu and by the scripts; this page explains them, it does not redefine them.

| Layer | Directory | Lifetime | Applied by |
|---|---|---|---|
| 0 bootstrap | `infra/tofu/bootstrap` | whole project | Identity Center **admin** profile (it creates IAM) |
| 1 network | `infra/tofu/network` | whole project (free) | `shopflow` operator profile |
| 2 cluster | `infra/tofu/cluster` | one session | `make cloud-up` / `make cloud-down` |

## First AWS session: the order

Each **approval** below is the user's, relayed by the orchestrator. Costs are estimates from [`docs/cost.md`](../cost.md).

| # | Step | Who | Time | Cost | Details |
|---|---|---|---|---|---|
| 1 | Note the account creation date; **upgrade to the Paid plan**; root MFA; Identity Center user, permission sets, SSO profiles, `aws sso login` | user | ~45 min | $0 | [One-time account setup](#one-time-account-setup-user) |
| 2 | GitHub: OIDC `sub` customization, variable `AWS_ACCOUNT_ID`, secret `ALERT_EMAIL` | user (repo admin) | 5 min | $0 | [GitHub settings](#github-settings-user-repository-admin) |
| 3 | **Approval 1.** Layer 0 with the admin profile: local state → apply → migrate the state to S3. Confirm the SNS email. If the anomaly monitor already exists, reuse it | sf-cloud + user | ~15 min | ≈ $0.5/month from now on | [Bootstrap layer 0](#bootstrap-layer-0-first-time) |
| 4 | **Approval 2.** Layer 1 with the operator profile | sf-cloud | ~3 min | $0 | same section |
| 5 | Seed the secrets from a JSON file kept outside the repo, then delete the file | user | 10 min | $0 | `make cloud-seed-params` |
| 6 | Arm the guards: uncomment the reaper cron (PR), run it once with `dry_run: true`, set `INFRA_PLAN_ENABLED=true` | sf-cloud + orchestrator | 10 min | $0 | [Kill switches](#kill-switches) |
| 7 | `make cloud-up CLOUD_ARGS="--dry-run --profiles core,aws"`: reads AWS, prints the plan, changes nothing | sf-cloud | 5 min | $0 | [Session lifecycle](#session-lifecycle) |
| 8 | **Approval 3.** `make cloud-up CLOUD_ARGS="--profiles core,aws"` (`aws`: ESO, LB controller, OpenCost), then the first-session checks | sf-cloud | 30–45 min to RTO | ≈ $0.30/h (core only) | [First-session checks](#first-session-checks) |
| 9 | `make cloud-down` (same day); the orphan check must be clean | sf-cloud | ~20 min | — | |
| 10 | Next day: Cost Explorer with the Credit charge type excluded → measured $/hour in `docs/cost.md`; then activate the cost allocation tags | sf-cloud | 15 min | $0 | [`docs/cost.md`](../cost.md) |

Expected spend for the first session: about **$1.2 for 4 hours** (core profile), plus layer 0 at ≈ $0.5/month.
Guardrails live from step 3: emails at $10 and $20 actual and $25 forecast; the Budget Action at $25 blocks new
capacity; the lease (4 h by default) with the GitHub and Lambda reapers is the real stop.

Before step 8 these must be merged (other lanes): the sf-platform root-app hook with the session parameters
(ADR 0206) and the `aws` overlays of the core components, including the Envoy Gateway Service with the NLB
load balancer class.

## One-time account setup (user)

- [ ] Note the account creation date in [`docs/cost.md`](../cost.md).
- [ ] **Upgrade to the Paid plan** (Billing console → Plan). Credits then last until 12 months after account
      creation; anything beyond is charged to the card. Do this before creating layer 0.
- [ ] Root user: enable **MFA**; never use root again except for account-level tasks.
- [ ] Enable **IAM Identity Center** in `ap-southeast-1`; create your user (MFA required at sign-in).
- [ ] Permission set `ShopflowAdmin` = `AdministratorAccess`, session 1 h. Used only for layer 0.
- [ ] Permission set `ShopflowOperator` with only this inline policy (the operator role trusts exactly this
      permission set):

      ```json
      {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
        "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/shopflow-operator"}]}
      ```

- [ ] Assign both permission sets to your user for this account.
- [ ] AWS CLI v2 profiles in `~/.aws/config`:

      ```ini
      [sso-session shopflow]
      sso_start_url = https://<your-portal>.awsapps.com/start
      sso_region = ap-southeast-1
      sso_registration_scopes = sso:account:access

      [profile shopflow-admin]
      sso_session = shopflow
      sso_account_id = <ACCOUNT_ID>
      sso_role_name = ShopflowAdmin
      region = ap-southeast-1

      [profile shopflow-sso]
      sso_session = shopflow
      sso_account_id = <ACCOUNT_ID>
      sso_role_name = ShopflowOperator
      region = ap-southeast-1

      [profile shopflow]
      role_arn = arn:aws:iam::<ACCOUNT_ID>:role/shopflow-operator
      source_profile = shopflow-sso
      region = ap-southeast-1
      ```

- [ ] `aws sso login --sso-session shopflow`, then `aws sts get-caller-identity --profile shopflow` once layer 0
      exists. Role chaining caps operator sessions at 1 h; the CLI refreshes them on its own.

## GitHub settings (user; repository admin)

- [ ] OIDC `sub` claim with the workflow file, so each AWS role trusts one workflow (ADR 0509):

      ```sh
      gh api -X PUT repos/winthebest/shopflow/actions/oidc/customization/sub \
        -F use_default=false -f 'include_claim_keys[]=repo' -f 'include_claim_keys[]=context' \
        -f 'include_claim_keys[]=job_workflow_ref'
      ```

- [ ] Repository variable `AWS_ACCOUNT_ID`; secret `ALERT_EMAIL` (used by the plan job).
- [ ] Variable `INFRA_PLAN_ENABLED=true` only after layer 0 exists (turns on `tofu plan` in infra-ci).

## Bootstrap layer 0 (first time)

The state bucket is created by this layer, so the first apply runs on local state and then migrates.

```sh
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
- [ ] After the first session has run for ~24 h: `TF_VAR_activate_cost_allocation_tags=true`, apply again.

Layer 1, with the operator profile:

```sh
export AWS_PROFILE=shopflow TF_VAR_aws_account_id=<ACCOUNT_ID>
tofu -chdir=infra/tofu/network init -backend-config="bucket=shopflow-tfstate-<ACCOUNT_ID>"
tofu -chdir=infra/tofu/network apply
```

Secrets for External Secrets (SecureString, never on a command line):

```sh
make cloud-seed-params < secrets.json                  # {"<namespace>": {"<name>": "<value>" or {"<key>": "<value>"}}}
make cloud-seed-params CLOUD_ARGS=--rotate < new.json  # overwrite the ones given
scripts/data-secrets.sh --aws-json | scripts/aws-seed-params.sh   # sf-data's secrets, with their groups
```

Values that must match each other (a password and the bcrypt hash of it in another Secret) are declared by the
producer as a group, `"_groups": [["lakehouse/trino-dbt", "lakehouse/trino-password-db"]]`. A group is written
whole or not at all: if SSM holds only part of it and `--rotate` is not given, the script stops before writing any
parameter.

## Session lifecycle

| Command | What it does |
|---|---|
| `make cloud-up` | preflight (operator role, Budget Action not fired, EKS version in standard support, no active lease, backup chain) → provisional lease → apply layer 2 → kubeconfig → Argo CD (chart + values from `deploy/argocd/bootstrap/`) → root apps through `scripts/platform-root-apps.sh --overlay aws` → wait Synced/Healthy → Postgres chain → CDC epoch → smoke (a checkout through the NLB is in `lake_ro` `bronze.orders` with this session's `_cdc_epoch` within 2 min, queried as the read-only `exporter` user; the password goes through stdin) → prints RTO-infra and RTO-service → final lease + Lambda schedule → re-enables the GitHub reaper |
| `make cloud-up CLOUD_ARGS=--dry-run` | reads AWS, runs `tofu plan`, prints every change instead of making it |
| `make cloud-up CLOUD_ARGS=--resume` | continues a cloud-up that stopped (reuses the recorded session plan) |
| `make cloud-up CLOUD_ARGS="--pitr 2026-11-02T10:15:00Z"` | restores Postgres to that time |
| `make cloud-extend HOURS=2` | lease + 2 h (max 8 h from now) and moves the Lambda schedule; nothing else |
| `make cloud-pause` / `make cloud-resume` | node group to 0 and back (< 4 h breaks; control plane, NLB and volumes still bill ~$0.16/h) |
| `make cloud-down` | auto-sync off → final backup + pointer → evidence to S3 → Gateway/LB deleted, wait for ELBv2 → stateful CRs + PVCs deleted, wait for EBS → uninstall Argo CD → `tofu destroy` layer 2 (3 tries) → orphan check in every region → drop lease, session, schedule. Re-run it after any interruption |
| `make cloud-down CLOUD_ARGS=--force-api` | paused or unreachable cluster: AWS API teardown like the reapers, no final backup |

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

External Secrets (`deploy/platform/external-secrets/aws/secret-stores`): one ClusterSecretStore `ssm-<namespace>`
per namespace in `eso_namespaces`, usable only from that namespace (`conditions`), assuming
`shopflow-eso-<namespace>`, which reads only `/shopflow/aws/<namespace>/*`. Its `secrets` list maps each SSM
parameter to a Kubernetes Secret with the same name and keys as the local KSOPS Secret, so workloads do not change:

| Secret | SSM parameter (SecureString unless noted) | Seed value |
|---|---|---|
| `observability/grafana-admin` | `/shopflow/aws/observability/grafana-admin` | `{"admin-user": "...", "admin-password": "..."}` |
| `observability/alertmanager-webhook` | `/shopflow/aws/observability/alertmanager-webhook` | `{"url": "..."}` |
| `shop/shop-db-debezium`, `shop/shop-db-trino-pg`, `shop/shop-db-polaris` | `/shopflow/aws/shop/<name>` | `{"username": "...", "password": "..."}` |
| `lakehouse/trino-exporter` | `/shopflow/aws/lakehouse/trino-exporter` | `{"password": "..."}` |
| `kafka/cdc-epoch` (key `epoch`) | `/shopflow/aws/kafka/cdc-epoch` (String, written by cloud-up) | — |

The shop-db role passwords must keep their first values: the roles come back with every restored database.
`scripts/cloud-manifests-check.sh` (`make cloud-manifests`) fails if this chart drifts from the contract.

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
have no final backup: RPO is the WAL `archive_timeout` (default 5 minutes).

## Kill switches

| Switch | Where | When | How |
|---|---|---|---|
| GitHub reaper | `.github/workflows/cloud-reaper.yml` → `scripts/cloud-reap.sh` | hourly at :17 (once armed) and on dispatch | takes the layer-2 state lock, re-reads the lease, deletes NLBs → node groups → available EBS through AWS APIs, `tofu destroy -lock=false` under its lock, orphan check. Role `shopflow-reaper` can delete only `project=shopflow` resources |
| Lambda reaper | layer 0 (`shopflow-reaper`) + EventBridge Scheduler `shopflow/shopflow-reaper` | every 15 min from lease + 1 h | same API teardown, then deletes the cluster and its own schedule. Independent of GitHub (which disables cron after 60 days of repo inactivity) |

A missing or unreadable lease counts as expired. Failures alert: a failed workflow run (GitHub email), the
Lambda error alarm (SNS email). The GitHub reaper also fails, and so alerts, when the state lock is older than
3 h or when the cluster is more than 1 h past its lease while the state is locked.

**Arming the GitHub reaper** (first approved session): uncomment the `schedule` block in
`cloud-reaper.yml` in a PR, and run the workflow once by hand with `dry_run: true`.

Tests to run in the first sessions (Phase 6 step 8): a 10-minute lease → the GitHub reaper destroys the
cluster; reaper workflow disabled → the Lambda destroys it; a paused cluster → still destroyed.

## First-session checks

- [ ] `aws eks describe-cluster-versions`: bump `kubernetes_version` in the contract if 1.35 is not the newest
      standard-support version.
- [ ] `aws eks describe-addon-configuration --addon-name vpc-cni` / `aws-ebs-csi-driver`: confirm
      `enableNetworkPolicy`, `env.ADDITIONAL_ENI_TAGS` and `controller.extraVolumeTags` are in the schema.
- [ ] Spot capacity in AZ-a for the instance types; add types if the node group stays `CREATE_FAILED`/pending.
- [ ] The Envoy Gateway Service (sf-platform aws overlay) sets `loadBalancerClass: service.k8s.aws/nlb` itself.
      Otherwise, if it is created before the LB controller's webhook is up, EKS's legacy cloud provider builds a
      Classic ELB without the `project` tag, which the reapers cannot delete.
- [ ] `kubectl get clustersecretstores,externalsecrets -A`: every store `Valid`, every ExternalSecret
      `SecretSynced`. The usual causes are a missing `aws.accountId` parameter or an unseeded parameter.
- [ ] OpenCost (`kubectl -n opencost port-forward svc/opencost 9090`) shows cost per namespace.
- [ ] Measure $/hour (full stack and paused) and fill [`docs/cost.md`](../cost.md).

## Troubleshooting

| Symptom | Action |
|---|---|
| cloud-down step 4/5 over 10 minutes (finalizers, LB) | re-run `make cloud-down`; still stuck → `CLOUD_ARGS=--force-api`; write a postmortem |
| `tofu destroy` keeps failing | `make cloud-down` again (it resumes); check `aws eks list-nodegroups`, ENIs in use, then the orphan check |
| Orphan check exit 2 | an untagged resource exists somewhere: identify it by hand; the scripts never delete it |
| Orphan check: `UNTAGGED … load-balancer` | a load balancer in the shopflow VPC without `project=shopflow`, typically a Classic ELB made by EKS's legacy cloud provider for a Service without the NLB class. Reapers cannot delete it (their IAM needs the tag). Check its tags and listeners, delete it (`aws elb delete-load-balancer` / `aws elbv2 delete-load-balancer`), and fix the Service that caused it. cloud-down stays failed and the hourly reaper keeps alerting until it is gone |
| Orphan check exit 3 | shopflow-tagged leftovers outside the home region or an EKS cluster: `cloud-down --force-api`, or delete by hand |
| Reaper: "state lock is old" | a run died holding `cluster/terraform.tfstate.tflock`; confirm nothing runs, delete the object |
| cloud-up: "Budget Action has fired" | spend passed $25: review `docs/cost.md`, raise `action_threshold_usd` in layer 0, then reset the action in the Budgets console |
| cloud-up: EKS version not in standard support | bump `kubernetes_version` in `infra/cloud-contract.json` |
