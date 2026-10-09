output "role_arn" {
  description = "ARN of the role."
  value       = aws_iam_role.this.arn
}

output "role_name" {
  description = "Name of the role."
  value       = aws_iam_role.this.name
}

output "permissions_boundary" {
  description = "Permissions boundary attached to the role."
  value       = aws_iam_role.this.permissions_boundary
}

output "trust_policy" {
  description = "Trust policy document (JSON)."
  value       = aws_iam_role.this.assume_role_policy
}

output "policy" {
  description = "Inline permissions policy document (JSON)."
  value       = aws_iam_role_policy.this.policy
}
