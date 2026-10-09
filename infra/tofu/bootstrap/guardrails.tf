module "cost_guardrails" {
  source = "../modules/cost-guardrails"

  name_prefix              = local.project
  account_id               = local.account_id
  alert_email              = var.alert_email
  permissions_boundary_arn = aws_iam_policy.boundary.arn

  budget_limit_usd              = var.budget_limit_usd
  budget_start                  = var.budget_start
  budget_end                    = var.budget_end
  action_threshold_usd          = var.action_threshold_usd
  anomaly_monitor_arn           = var.anomaly_monitor_arn
  activate_cost_allocation_tags = var.activate_cost_allocation_tags

  # The operator starts sessions; the LB controller creates NLBs on its own.
  action_target_role_names = [aws_iam_role.operator.name, module.pod_role["aws-lb-controller"].role_name]
}

module "reaper_lambda" {
  source = "../modules/reaper-lambda"

  function_name            = local.contract.reaper.function_name
  region                   = local.region
  account_id               = local.account_id
  project                  = local.project
  cluster_name             = local.cluster
  lease_parameter_name     = local.contract.ssm.lease
  schedule_group_name      = local.contract.reaper.schedule_group
  schedule_name            = local.contract.reaper.schedule_name
  alert_topic_arn          = module.cost_guardrails.alert_topic_arn
  permissions_boundary_arn = aws_iam_policy.boundary.arn
  source_dir               = "${path.module}/../../lambda/reaper/src"
  role_name                = local.roles.reaper_lambda
  scheduler_role_name      = local.roles.reaper_scheduler
}
