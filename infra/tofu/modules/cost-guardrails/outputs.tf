output "alert_topic_arn" {
  description = "SNS topic for budget, anomaly and reaper alerts."
  value       = aws_sns_topic.alerts.arn
}

output "budget_name" {
  description = "Name of the cost budget (used by cloud-up to check the Budget Action status)."
  value       = aws_budgets_budget.total.name
}

output "budget_action_id" {
  description = "ID of the Budget Action that attaches the deny policy."
  value       = aws_budgets_budget_action.deny_create.action_id
}

output "deny_policy_arn" {
  description = "Policy attached by the Budget Action."
  value       = aws_iam_policy.deny_create.arn
}

output "budget_action_role_arn" {
  description = "Execution role of the Budget Action."
  value       = aws_iam_role.budget_action.arn
}

output "budget_action_role_boundary" {
  description = "Permissions boundary of the Budget Action role."
  value       = aws_iam_role.budget_action.permissions_boundary
}

output "budget_action_target_roles" {
  description = "Roles that receive the deny policy when the Budget Action fires."
  value       = one(one(aws_budgets_budget_action.deny_create.definition).iam_action_definition).roles
}
