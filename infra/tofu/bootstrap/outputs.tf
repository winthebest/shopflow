output "state_bucket" {
  description = "Bucket holding OpenTofu state for all layers."
  value       = aws_s3_bucket.this["state"].bucket
}

output "data_bucket" {
  description = "Lakehouse, Postgres backup, Flink checkpoint and evidence bucket."
  value       = aws_s3_bucket.this["data"].bucket
}

output "glue_databases" {
  description = "Glue databases backing the schemas of the Trino catalog lake."
  value       = sort([for db in aws_glue_catalog_database.lake : db.name])
}

output "operator_role_arn" {
  description = "Role assumed by the operator profile."
  value       = aws_iam_role.operator.arn
}

output "github_role_arns" {
  description = "Roles assumed by GitHub Actions through OIDC."
  value = {
    ci_plan = module.ci_plan_role.role_arn
    reaper  = module.reaper_role.role_arn
  }
}

output "pod_role_arns" {
  description = "Pod Identity role per contract key (used by the cluster layer)."
  value       = { for key, role in module.pod_role : key => role.role_arn }
}

output "alert_topic_arn" {
  description = "SNS topic for budget, anomaly and reaper alerts."
  value       = module.cost_guardrails.alert_topic_arn
}

output "budget_name" {
  description = "Budget whose action cloud-up checks before starting a session."
  value       = module.cost_guardrails.budget_name
}

output "reaper" {
  description = "Backup reaper wiring used by cloud-up/cloud-extend to (re)schedule it."
  value = {
    function_arn       = module.reaper_lambda.function_arn
    scheduler_role_arn = module.reaper_lambda.scheduler_role_arn
    schedule_group     = module.reaper_lambda.schedule_group_name
  }
}

output "role_boundaries" {
  description = "Permissions boundary of every role this layer creates (every value must be the boundary policy)."
  value = merge(
    {
      (aws_iam_role.operator.name)    = aws_iam_role.operator.permissions_boundary
      (aws_iam_role.eks_cluster.name) = aws_iam_role.eks_cluster.permissions_boundary
      (aws_iam_role.eks_node.name)    = aws_iam_role.eks_node.permissions_boundary
      (module.ci_plan_role.role_name) = module.ci_plan_role.permissions_boundary
      (module.reaper_role.role_name)  = module.reaper_role.permissions_boundary
      (local.roles.budget_action)     = module.cost_guardrails.budget_action_role_boundary
    },
    { for key, role in module.pod_role : role.role_name => role.permissions_boundary },
    { for ns, role in aws_iam_role.eso_namespace : role.name => role.permissions_boundary },
    module.reaper_lambda.role_boundaries,
  )
}
