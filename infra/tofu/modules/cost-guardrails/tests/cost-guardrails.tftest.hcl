# Mocked ARNs must look like ARNs: the provider still validates them.
mock_provider "aws" {
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-southeast-1:123456789012:shopflow-alerts" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/shopflow-budget-action" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/shopflow-budget-deny" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/shopflow-services" }
  }
}

variables {
  name_prefix              = "shopflow"
  account_id               = "123456789012"
  alert_email              = "alerts@example.com"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/shopflow-boundary"
  action_target_role_names = ["shopflow-operator", "shopflow-pod-aws-lb-controller"]
}

run "budget_excludes_credits_and_refunds" {
  command = apply

  assert {
    condition     = aws_budgets_budget.total.time_unit == "CUSTOM" && aws_budgets_budget.total.time_period_start == "2026-10-01_00:00" && aws_budgets_budget.total.time_period_end == "2027-10-01_00:00"
    error_message = "One cumulative window for the whole project: thresholds must not reset at a month or year boundary."
  }

  assert {
    condition     = one(aws_budgets_budget.total.cost_types).include_credit == false && one(aws_budgets_budget.total.cost_types).include_refund == false
    error_message = "Credits are applied before Budgets evaluates spend; a budget that counts them never alerts."
  }

  assert {
    condition = toset([for n in aws_budgets_budget.total.notification : "${n.notification_type}:${n.threshold}"]) == toset([
      "ACTUAL:10", "ACTUAL:20", "FORECASTED:25",
    ])
    error_message = "Emails at 10/20 USD actual and 25 USD forecast."
  }

  assert {
    condition     = alltrue([for n in aws_budgets_budget.total.notification : n.threshold_type == "ABSOLUTE_VALUE"])
    error_message = "Thresholds are in USD, not percent of the limit."
  }
}

run "budget_action_attaches_deny_policy_at_25_usd" {
  command = apply

  assert {
    condition     = one(aws_budgets_budget_action.deny_create.action_threshold).action_threshold_value == 25
    error_message = "The Budget Action fires at 25 USD actual spend."
  }

  assert {
    condition     = aws_budgets_budget_action.deny_create.approval_model == "AUTOMATIC" && aws_budgets_budget_action.deny_create.action_type == "APPLY_IAM_POLICY"
    error_message = "The action applies the IAM policy without manual approval."
  }

  assert {
    condition     = toset(one(one(aws_budgets_budget_action.deny_create.definition).iam_action_definition).roles) == toset(var.action_target_role_names)
    error_message = "The deny policy targets the operator and the load balancer controller."
  }

  assert {
    condition = alltrue([
      for a in jsondecode(aws_iam_policy.deny_create.policy).Statement[0].Action : !can(regex("Delete|Terminate|UpdateNodegroupConfig", a))
    ])
    error_message = "The deny policy must not block teardown or scale-down."
  }

  assert {
    condition     = contains(jsondecode(aws_iam_policy.deny_create.policy).Statement[0].Action, "eks:CreateCluster")
    error_message = "The deny policy blocks a new session."
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.budget_action.policy).Statement[0].Condition.ArnEquals["iam:PolicyARN"] == aws_iam_policy.deny_create.arn
    error_message = "The execution role can attach only the deny policy."
  }

  assert {
    condition     = aws_iam_role.budget_action.permissions_boundary == var.permissions_boundary_arn
    error_message = "Every role carries the permissions boundary."
  }
}

run "anomaly_subscription_is_immediate_to_sns" {
  command = apply

  assert {
    condition     = aws_ce_anomaly_subscription.immediate.frequency == "IMMEDIATE" && one(aws_ce_anomaly_subscription.immediate.subscriber).address == aws_sns_topic.alerts.arn
    error_message = "Each anomaly is sent individually through SNS."
  }

  assert {
    condition     = length(aws_ce_anomaly_monitor.services) == 1
    error_message = "A services monitor is created when none is supplied."
  }

  assert {
    condition     = length(aws_ce_cost_allocation_tag.this) == 0
    error_message = "Cost allocation tags are activated only after the tag keys exist in billing data."
  }
}

run "reuses_existing_anomaly_monitor" {
  command = apply

  variables {
    anomaly_monitor_arn           = "arn:aws:ce::123456789012:anomalymonitor/existing"
    activate_cost_allocation_tags = true
  }

  assert {
    condition     = length(aws_ce_anomaly_monitor.services) == 0 && aws_ce_anomaly_subscription.immediate.monitor_arn_list == tolist(["arn:aws:ce::123456789012:anomalymonitor/existing"])
    error_message = "An existing monitor is reused instead of creating a second one."
  }

  assert {
    condition     = toset(keys(aws_ce_cost_allocation_tag.this)) == toset(["project", "env"])
    error_message = "project and env are activated as cost allocation tags."
  }
}
