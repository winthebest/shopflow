locals {
  # Shared with the network and cluster layers, the cloud scripts and the Helm values.
  contract = jsondecode(file("${path.module}/../../cloud-contract.json"))

  project    = local.contract.project
  region     = local.contract.region
  account_id = var.aws_account_id
  cluster    = local.contract.cluster_name
  roles      = local.contract.roles
  prefixes   = local.contract.data_prefixes

  state_bucket = "${local.contract.state_bucket_prefix}${local.account_id}"
  data_bucket  = "${local.contract.data_bucket_prefix}${local.account_id}"

  github_repository = local.contract.github_repository
  workflows = {
    ci_plan = ".github/workflows/infra-ci.yml"
    reaper  = ".github/workflows/cloud-reaper.yml"
  }

  arn = {
    account      = "arn:aws:iam::${local.account_id}:root"
    role         = "arn:aws:iam::${local.account_id}:role"
    policy       = "arn:aws:iam::${local.account_id}:policy"
    cluster      = "arn:aws:eks:${local.region}:${local.account_id}:cluster/${local.cluster}"
    parameter    = "arn:aws:ssm:${local.region}:${local.account_id}:parameter"
    state_bucket = "arn:aws:s3:::${local.state_bucket}"
    data_bucket  = "arn:aws:s3:::${local.data_bucket}"
    glue         = "arn:aws:glue:${local.region}:${local.account_id}"
    reaper_fn    = "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.contract.reaper.function_name}"
    schedules    = "arn:aws:scheduler:${local.region}:${local.account_id}:schedule/${local.contract.reaper.schedule_group}/*"
  }

  boundary_name    = "${local.project}-boundary"
  deny_policy_name = "${local.project}-budget-deny"

  operator_principal_arns = coalesce(var.operator_principal_arns, [
    "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_ShopflowOperator_*",
  ])

  # Pod roles reached through Pod Identity, keyed like contract.pod_identities.
  pod_role_names = { for key, _ in local.contract.pod_identities : key => "${local.roles.pod_prefix}${key}" }
}
