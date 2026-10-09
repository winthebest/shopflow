# Mocked ARNs must look like ARNs: the provider still validates them.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/shopflow-reaper-lambda" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/lambda/shopflow-reaper" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ap-southeast-1:123456789012:function:shopflow-reaper" }
  }
}

variables {
  function_name            = "shopflow-reaper"
  region                   = "ap-southeast-1"
  account_id               = "123456789012"
  project                  = "shopflow"
  cluster_name             = "shopflow"
  lease_parameter_name     = "/shopflow/aws/control/lease-expires-at"
  schedule_group_name      = "shopflow"
  schedule_name            = "shopflow-reaper"
  alert_topic_arn          = "arn:aws:sns:ap-southeast-1:123456789012:shopflow-alerts"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/shopflow-boundary"
  source_dir               = "../../../lambda/reaper/src"
  role_name                = "shopflow-reaper-lambda"
  scheduler_role_name      = "shopflow-reaper-scheduler"
}

run "function_is_configured_for_the_session_cluster" {
  command = apply

  assert {
    condition     = aws_lambda_function.this.handler == "reaper.handler.lambda_handler" && aws_lambda_function.this.timeout == 900
    error_message = "Handler points at the reaper package and uses the maximum timeout."
  }

  assert {
    condition = aws_lambda_function.this.environment[0].variables == tomap({
      REAPER_CLUSTER_NAME    = "shopflow"
      REAPER_PROJECT         = "shopflow"
      REAPER_LEASE_PARAMETER = "/shopflow/aws/control/lease-expires-at"
      REAPER_SCHEDULE_GROUP  = "shopflow"
      REAPER_SCHEDULE_NAME   = "shopflow-reaper"
      REAPER_ALERT_TOPIC_ARN = "arn:aws:sns:ap-southeast-1:123456789012:shopflow-alerts"
    })
    error_message = "The function receives its target and lease through the environment."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.errors.alarm_actions == toset([var.alert_topic_arn])
    error_message = "Reaper failures alert through SNS."
  }

  assert {
    condition     = alltrue([for b in values(output.role_boundaries) : b == var.permissions_boundary_arn])
    error_message = "Every role carries the permissions boundary."
  }
}

run "deletes_are_limited_to_project_tagged_resources" {
  command = apply

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.lambda.policy).Statement :
      try(s.Condition.StringEquals["aws:ResourceTag/project"] == "shopflow", false)
      if length([for a in flatten([s.Action]) : a if can(regex(":Delete", a)) && a != "scheduler:DeleteSchedule"]) > 0
    ])
    error_message = "Every delete permission (except its own schedule) requires project=shopflow on the resource."
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.lambda.policy).Statement : s
      if length([for a in flatten([s.Action]) : a if can(regex("^iam:", a))]) > 0
    ]) == 0
    error_message = "The reaper has no IAM permissions."
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.scheduler.policy).Statement[0].Resource == aws_lambda_function.this.arn
    error_message = "The scheduler role can invoke only the reaper."
  }
}
