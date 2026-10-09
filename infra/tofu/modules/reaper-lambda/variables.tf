variable "function_name" {
  description = "Lambda function name."
  type        = string
}

variable "region" {
  description = "Region of the cluster and of the function."
  type        = string
}

variable "account_id" {
  description = "AWS account ID."
  type        = string
}

variable "project" {
  description = "Value of the `project` tag that marks resources the reaper may delete."
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster torn down when the lease has expired."
  type        = string
}

variable "lease_parameter_name" {
  description = "SSM parameter holding the lease expiry (ISO 8601 UTC). Missing or unreadable means expired."
  type        = string
}

variable "schedule_group_name" {
  description = "EventBridge Scheduler group that holds the per-session reaper schedule."
  type        = string
}

variable "schedule_name" {
  description = "Schedule that cloud-up creates at lease + grace; the function deletes it once nothing is left."
  type        = string
}

variable "alert_topic_arn" {
  description = "SNS topic for reaper results and failures."
  type        = string
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary attached to both roles."
  type        = string
}

variable "source_dir" {
  description = "Directory that contains the `reaper` Python package."
  type        = string
}

variable "role_name" {
  description = "Execution role of the function."
  type        = string
}

variable "scheduler_role_name" {
  description = "Role EventBridge Scheduler assumes to invoke the function."
  type        = string
}

variable "log_retention_days" {
  description = "Retention of the function's log group."
  type        = number
  default     = 30
}
