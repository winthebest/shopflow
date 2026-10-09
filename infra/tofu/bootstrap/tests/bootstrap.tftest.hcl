# Offline checks of layer 0 with a mocked AWS provider: no credentials, no API calls.
# Mocked ARNs must look like ARNs because the provider still validates them.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-southeast-1:123456789012:shopflow-alerts" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-southeast-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ap-southeast-1:123456789012:function:shopflow-reaper" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/mock" }
  }
}

override_resource {
  target = aws_iam_policy.boundary
  values = { arn = "arn:aws:iam::123456789012:policy/shopflow-boundary" }
}

override_resource {
  target = module.cost_guardrails.aws_iam_policy.deny_create
  values = { arn = "arn:aws:iam::123456789012:policy/shopflow-budget-deny" }
}

variables {
  aws_account_id = "123456789012"
  alert_email    = "alerts@example.com"
}

run "every_role_carries_the_boundary" {
  command = plan

  assert {
    condition     = alltrue([for b in values(output.role_boundaries) : b == "arn:aws:iam::123456789012:policy/shopflow-boundary"])
    error_message = "Every role must carry the shopflow-boundary permissions boundary."
  }

  assert {
    # operator, eks cluster/node, ci-plan, reaper, budget action, 7 pod roles, 6 ESO namespace roles, Lambda + scheduler
    condition     = length(output.role_boundaries) == 21
    error_message = "A role was added without being listed in role_boundaries."
  }
}

run "boundary_keeps_work_in_one_region_and_protects_guardrails" {
  command = plan

  assert {
    condition = one([
      for s in jsondecode(aws_iam_policy.boundary.policy).Statement : s.Condition.StringNotEquals["aws:RequestedRegion"] if s.Sid == "DenyOtherRegions"
    ]) == ["ap-southeast-1"]
    error_message = "Everything outside ap-southeast-1 is denied."
  }

  assert {
    condition = alltrue([for a in ["ec2:Describe*", "elasticloadbalancing:Describe*", "iam:*", "budgets:*", "shield:Get*"] : contains(one([
      for s in jsondecode(aws_iam_policy.boundary.policy).Statement : s.NotAction if s.Sid == "DenyOtherRegions"
    ]), a)])
    error_message = "Global services and the all-region orphan check must stay allowed."
  }

  assert {
    condition = one([
      for s in jsondecode(aws_iam_policy.boundary.policy).Statement : s.Condition.ArnEquals["iam:PolicyARN"] if s.Sid == "OnlyBudgetActionDetachesItsPolicy"
    ]) == module.cost_guardrails.deny_policy_arn
    error_message = "The boundary must protect the exact policy the Budget Action attaches."
  }

  assert {
    condition     = length(aws_iam_policy.boundary.policy) <= 6144
    error_message = "Managed policies are limited to 6144 characters."
  }
}

run "operator_is_fenced" {
  command = plan

  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_policy.operator.policy).Statement : s.Condition.StringNotEquals["ec2:InstanceType"] if s.Sid == "DenyDirectInstancesOutsideAllowList"
    ])) == toset(jsondecode(file("../../cloud-contract.json")).allowed_instance_types)
    error_message = "Direct RunInstances is limited to the contract's instance types."
  }

  assert {
    condition = one([
      for s in jsondecode(aws_iam_policy.operator.policy).Statement : s.Condition.StringNotEquals["aws:RequestTag/project"] if s.Sid == "DenyDirectInstancesWithoutProjectTag"
    ]) == "shopflow"
    error_message = "Direct RunInstances needs the project tag."
  }

  assert {
    condition = length([
      for a in flatten([for s in jsondecode(aws_iam_policy.operator.policy).Statement : flatten([s.Action]) if s.Effect == "Allow"]) : a
      if startswith(a, "iam:") && !contains(["iam:Get*", "iam:List*", "iam:PassRole", "iam:CreateServiceLinkedRole"], a)
    ]) == 0
    error_message = "The operator must not be able to change IAM."
  }

  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_policy.operator.policy).Statement : s.Action if s.Sid == "DenyCostTraps"]), "ec2:CreateNatGateway")
    error_message = "NAT gateways are denied (no-NAT design)."
  }

  assert {
    condition     = strcontains(jsonencode(jsondecode(aws_iam_role.operator.assume_role_policy).Statement[0].Condition.ArnLike["aws:PrincipalArn"]), "AWSReservedSSO_ShopflowOperator_")
    error_message = "Only the Identity Center operator permission set may assume the role by default."
  }

  assert {
    condition     = length(aws_iam_policy.operator.policy) <= 6144
    error_message = "Managed policies are limited to 6144 characters."
  }
}

run "ci_plan_reads_but_never_secrets_or_data" {
  command = plan

  assert {
    condition     = jsondecode(module.ci_plan_role.trust_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] == ["repo:winthebest/shopflow:pull_request:job_workflow_ref:winthebest/shopflow/.github/workflows/infra-ci.yml@refs/pull/*/merge"]
    error_message = "ci-plan trusts only infra-ci.yml on pull requests of this repository."
  }

  assert {
    condition = toset(flatten([for s in jsondecode(module.ci_plan_role.policy).Statement : s.Action if s.Sid == "NeverReadSecrets"])) == toset([
      "ssm:GetParameter*", "kms:Decrypt", "secretsmanager:GetSecretValue",
    ])
    error_message = "ci-plan explicitly cannot read SSM parameters or decrypt."
  }

  assert {
    condition = length([
      for a in flatten([for s in jsondecode(module.ci_plan_role.policy).Statement : flatten([s.Action]) if s.Effect == "Allow"]) : a
      if can(regex(":(Put|Delete|Create|Update|Attach|Run|Terminate)", a))
    ]) == 0
    error_message = "ci-plan has no write permission."
  }
}

run "reaper_only_deletes_tagged_session_compute" {
  command = plan

  assert {
    condition     = jsondecode(module.reaper_role.trust_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == ["repo:winthebest/shopflow:ref:refs/heads/main:job_workflow_ref:winthebest/shopflow/.github/workflows/cloud-reaper.yml@refs/heads/main"]
    error_message = "The reaper role trusts only cloud-reaper.yml on main."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(module.reaper_role.policy).Statement : try(s.Condition.StringEquals["aws:ResourceTag/project"] == "shopflow", false)
      if s.Effect == "Allow" && length([for a in flatten([s.Action]) : a if can(regex("^(eks|ec2|elasticloadbalancing):(Delete|Terminate|Release|Disassociate)", a))]) > 0
    ])
    error_message = "Every delete requires project=shopflow on the resource."
  }

  assert {
    condition     = one([for s in jsondecode(module.reaper_role.policy).Statement : s.Resource if s.Sid == "LockAndWriteClusterStateOnly"]) == ["arn:aws:s3:::shopflow-tfstate-123456789012/cluster/terraform.tfstate", "arn:aws:s3:::shopflow-tfstate-123456789012/cluster/terraform.tfstate.tflock"]
    error_message = "The reaper writes and locks only the cluster layer state."
  }

  assert {
    condition     = toset(["iam:Create*", "iam:Attach*", "iam:PassRole"]) == setintersection(toset(one([for s in jsondecode(module.reaper_role.policy).Statement : s.Action if s.Sid == "NeverChangeIam"])), toset(["iam:Create*", "iam:Attach*", "iam:PassRole"]))
    error_message = "IAM create/attach/pass are explicitly denied."
  }
}

run "data_bucket_lifecycle_spares_postgres_backups" {
  command = plan

  assert {
    condition     = toset([for r in aws_s3_bucket_lifecycle_configuration.data.rule : one(r.filter).prefix]) == toset(["iceberg/", "flink-ckpt/", "evidence/"])
    error_message = "Only iceberg/, flink-ckpt/ and evidence/ expire noncurrent versions."
  }

  assert {
    condition     = alltrue([for r in aws_s3_bucket_lifecycle_configuration.data.rule : one(r.noncurrent_version_expiration).noncurrent_days == 7 && length(r.expiration) == 0])
    error_message = "Rules delete only noncurrent versions, after 7 days."
  }

  assert {
    condition     = alltrue([for v in aws_s3_bucket_versioning.this : one(v.versioning_configuration).status == "Enabled"])
    error_message = "State and data buckets are versioned."
  }

  assert {
    condition     = alltrue([for b in aws_s3_bucket_public_access_block.this : b.block_public_acls && b.block_public_policy && b.ignore_public_acls && b.restrict_public_buckets])
    error_message = "Block Public Access is fully on for both buckets."
  }

  assert {
    condition     = toset(output.glue_databases) == toset(["bronze", "silver", "gold"])
    error_message = "Glue databases match the lake schemas in the contract."
  }
}

run "pod_roles_are_scoped_by_prefix" {
  command = plan

  assert {
    condition     = toset(flatten([for s in jsondecode(module.pod_role["cnpg"].policy).Statement : s.Resource if s.Effect == "Allow" && strcontains(jsonencode(s.Action), "Object")])) == toset(["arn:aws:s3:::shopflow-data-123456789012/pg-backup/*"])
    error_message = "CNPG reads and writes only pg-backup/."
  }

  assert {
    condition     = contains([for s in jsondecode(module.pod_role["trino"].policy).Statement : s.Resource if s.Effect == "Deny"], "arn:aws:s3:::shopflow-data-123456789012/pg-backup/*")
    error_message = "Trino is explicitly denied the Postgres backups."
  }

  assert {
    condition     = !strcontains(module.pod_role["flink"].policy, "glue:")
    error_message = "Flink gets Glue only for the Iceberg-writer fallback."
  }

  assert {
    condition     = jsondecode(module.pod_role["cnpg"].trust_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "shop-db"
    error_message = "Pod roles are pinned to the contract's service account."
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.eso_namespace["shop"].policy).Statement[0].Resource == ["arn:aws:ssm:ap-southeast-1:123456789012:parameter/shopflow/aws/shop", "arn:aws:ssm:ap-southeast-1:123456789012:parameter/shopflow/aws/shop/*"]
    error_message = "The shop SecretStore role reads only /shopflow/aws/shop/*."
  }

  assert {
    condition     = toset(module.cost_guardrails.budget_action_target_roles) == toset(["shopflow-operator", "shopflow-pod-aws-lb-controller"])
    error_message = "The Budget Action fences the operator and the load balancer controller."
  }
}

run "flink_fallback_adds_iceberg_access" {
  command = plan

  variables {
    flink_writes_iceberg = true
  }

  assert {
    condition     = strcontains(module.pod_role["flink"].policy, "glue:UpdateTable") && strcontains(module.pod_role["flink"].policy, "/iceberg/*")
    error_message = "The fallback grants Glue and iceberg/* to Flink."
  }
}

run "rejects_malformed_account_id" {
  command = plan

  variables {
    aws_account_id = "1234"
  }

  expect_failures = [var.aws_account_id]
}
