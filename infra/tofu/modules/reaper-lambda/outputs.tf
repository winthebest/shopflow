output "function_arn" {
  description = "ARN of the reaper function (schedule target)."
  value       = aws_lambda_function.this.arn
}

output "function_name" {
  description = "Name of the reaper function."
  value       = aws_lambda_function.this.function_name
}

output "scheduler_role_arn" {
  description = "Role passed to EventBridge Scheduler when cloud-up creates the schedule."
  value       = aws_iam_role.scheduler.arn
}

output "schedule_group_name" {
  description = "Schedule group holding the per-session schedule."
  value       = aws_scheduler_schedule_group.this.name
}

output "role_boundaries" {
  description = "Permissions boundary of each role created by the module."
  value = {
    (aws_iam_role.lambda.name)    = aws_iam_role.lambda.permissions_boundary
    (aws_iam_role.scheduler.name) = aws_iam_role.scheduler.permissions_boundary
  }
}
