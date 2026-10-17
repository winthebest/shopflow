# Offline checks of layer 0 with a mocked AWS provider: no credentials, no API calls.
# Mocked ARNs must look like ARNs because the provider still validates them.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-southeast-2:123456789012:shopflow-alerts" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-southeast-2:123456789012:log-group:mock" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:ap-southeast-2:123456789012:shopflow-reaper-dlq" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ap-southeast-2:123456789012:function:shopflow-reaper" }
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
    condition     = length(output.role_boundaries) == 19
    error_message = "A role was added without being listed in role_boundaries."
  }
}

run "boundary_keeps_work_in_one_region_and_protects_guardrails" {
  command = plan

  assert {
    condition = one([
      for s in jsondecode(aws_iam_policy.boundary.policy).Statement : s.Condition.StringNotEquals["aws:RequestedRegion"] if s.Sid == "DenyOtherRegions"
    ]) == ["ap-southeast-2"]
    error_message = "Everything outside ap-southeast-2 is denied."
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
    condition     = jsondecode(aws_iam_role.operator.assume_role_policy).Statement[0].Condition.ArnLike["aws:PrincipalArn"] == ["arn:aws:iam::123456789012:role/managed/AccountFullAccessRole"]
    error_message = "By default only the account's `aws login` admin role may assume the operator role."
  }

  assert {
    condition     = length(aws_iam_policy.operator.policy) <= 6144
    error_message = "Managed policies are limited to 6144 characters."
  }
}

run "no_github_oidc_and_an_hourly_reaper_sweep" {
  command = plan

  assert {
    condition     = !strcontains(jsonencode(output.role_boundaries), "ci-plan") && !contains(keys(output.role_boundaries), "shopflow-reaper")
    error_message = "No role is trusted by GitHub Actions: project accounts deny OIDC providers (iam:*Provider*)."
  }

  assert {
    condition = alltrue([
      for st in jsondecode(aws_iam_policy.operator.policy).Statement : !contains(flatten([st.Resource]), "arn:aws:scheduler:ap-southeast-2:123456789012:schedule/shopflow/shopflow-reaper-sweep")
      && !contains(flatten([st.Resource]), "arn:aws:scheduler:ap-southeast-2:123456789012:schedule/shopflow/*")
      if st.Effect == "Allow" && length([for a in flatten([st.Action]) : a if contains(["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"], a)]) > 0
    ])
    error_message = "The operator may create, update or delete only the per-session schedule, never the sweep."
  }

  assert {
    condition = one([
      for st in jsondecode(aws_iam_policy.boundary.policy).Statement : st.Resource if st.Sid == "ProtectReaperSweep"
    ]) == "arn:aws:scheduler:ap-southeast-2:123456789012:schedule/shopflow/shopflow-reaper-sweep"
    error_message = "The boundary denies every role any change to the sweep."
  }

  assert {
    condition     = output.reaper.sweep_schedule == "shopflow-reaper-sweep"
    error_message = "Layer 0 keeps an hourly sweep that runs the reaper, independent of cloud-up."
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
    condition     = jsondecode(aws_iam_role_policy.eso_namespace["shop"].policy).Statement[0].Resource == ["arn:aws:ssm:ap-southeast-2:123456789012:parameter/shopflow/aws/shop", "arn:aws:ssm:ap-southeast-2:123456789012:parameter/shopflow/aws/shop/*"]
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
