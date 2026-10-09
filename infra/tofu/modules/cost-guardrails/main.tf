# Cost guardrails that keep working while the account spends credits.
# AWS applies credits before Budgets evaluates spend, so a budget that includes credits stays at
# $0 and never alerts: every budget here excludes credits and refunds. The budget covers one
# custom window (the project) so the USD thresholds are cumulative and never reset mid-project.

# Not KMS-encrypted on purpose: Cost Anomaly Detection and CloudWatch cannot publish to a topic
# encrypted with the AWS-managed SNS key, and a customer key costs $1/month. Alerts carry no secrets.
#trivy:ignore:AWS-0095
#trivy:ignore:AWS-0136
resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountOwnerManage"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
        Action    = ["sns:Publish", "sns:Subscribe", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:ListSubscriptionsByTopic"]
        Resource  = aws_sns_topic.alerts.arn
      },
      {
        Sid       = "CostAnomalyPublish"
        Effect    = "Allow"
        Principal = { Service = "costalerts.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = { StringEquals = { "aws:SourceAccount" = var.account_id } }
      },
      {
        Sid       = "CloudWatchAlarmsPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = { StringEquals = { "aws:SourceAccount" = var.account_id } }
      },
    ]
  })
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_budgets_budget" "total" {
  name              = "${var.name_prefix}-total"
  budget_type       = "COST"
  limit_amount      = format("%.2f", var.budget_limit_usd)
  limit_unit        = "USD"
  time_unit         = var.budget_time_unit
  time_period_start = var.budget_start
  time_period_end   = var.budget_end

  cost_types {
    include_credit = false
    include_refund = false
  }

  dynamic "notification" {
    for_each = toset(var.actual_alert_thresholds_usd)
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = var.forecast_alert_threshold_usd
    threshold_type             = "ABSOLUTE_VALUE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# Policy the Budget Action attaches; it only blocks creating billable capacity.
resource "aws_iam_policy" "deny_create" {
  name        = "${var.name_prefix}-budget-deny"
  description = "Attached by the Budget Action at ${var.action_threshold_usd} USD: blocks new billable capacity, keeps teardown working."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "DenyNewBillableCapacity"
      Effect   = "Deny"
      Action   = var.deny_create_actions
      Resource = "*"
    }]
  })
}

resource "aws_iam_role" "budget_action" {
  name                 = "${var.name_prefix}-budget-action"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "budget_action" {
  name = "${var.name_prefix}-budget-action"
  role = aws_iam_role.budget_action.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AttachOnlyTheDenyPolicy"
      Effect   = "Allow"
      Action   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
      Resource = [for r in var.action_target_role_names : "arn:aws:iam::${var.account_id}:role/${r}"]
      Condition = {
        ArnEquals = { "iam:PolicyARN" = aws_iam_policy.deny_create.arn }
      }
    }]
  })
}

resource "aws_budgets_budget_action" "deny_create" {
  budget_name        = aws_budgets_budget.total.name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action.arn

  action_threshold {
    action_threshold_type  = "ABSOLUTE_VALUE"
    action_threshold_value = var.action_threshold_usd
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.deny_create.arn
      roles      = var.action_target_role_names
    }
  }

  subscriber {
    address           = var.alert_email
    subscription_type = "EMAIL"
  }
}

resource "aws_ce_anomaly_monitor" "services" {
  count             = var.anomaly_monitor_arn == null ? 1 : 0
  name              = "${var.name_prefix}-services"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

# IMMEDIATE frequency sends each anomaly as it is detected (SNS is the only subscriber type allowed).
resource "aws_ce_anomaly_subscription" "immediate" {
  name             = "${var.name_prefix}-immediate"
  frequency        = "IMMEDIATE"
  monitor_arn_list = [coalesce(var.anomaly_monitor_arn, try(aws_ce_anomaly_monitor.services[0].arn, null))]

  subscriber {
    type    = "SNS"
    address = aws_sns_topic.alerts.arn
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = [tostring(var.anomaly_threshold_usd)]
    }
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

resource "aws_ce_cost_allocation_tag" "this" {
  for_each = var.activate_cost_allocation_tags ? toset(var.cost_allocation_tag_keys) : toset([])
  tag_key  = each.value
  status   = "Active"
}
