variable "aws_account_id" {
  description = "Account this layer may touch; the provider refuses any other credentials."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "alert_email" {
  description = "Receives budget, anomaly and reaper alerts. Pass with TF_VAR_alert_email; never commit it."
  type        = string
}

variable "operator_principal_arns" {
  description = "IAM principals (ArnLike patterns) allowed to assume shopflow-operator. Null means the IAM Identity Center permission set `ShopflowOperator` of this account."
  type        = list(string)
  default     = null
}

variable "budget_limit_usd" {
  description = "Budget limit for the whole period (the project credit cap)."
  type        = number
  default     = 100
}

variable "budget_start" {
  description = "Start of the cumulative budget window (YYYY-MM-DD_hh:mm, UTC)."
  type        = string
  default     = "2026-10-01_00:00"
}

variable "budget_end" {
  description = "End of the budget window (UTC); keep it past the credit expiry."
  type        = string
  default     = "2027-10-01_00:00"
}

variable "action_threshold_usd" {
  description = "Actual spend at which the Budget Action blocks new capacity. Raise only after reviewing docs/cost.md."
  type        = number
  default     = 25
}

variable "anomaly_monitor_arn" {
  description = "Existing Cost Anomaly Detection services monitor to reuse (new accounts get one from AWS). Null creates one."
  type        = string
  default     = null
}

variable "activate_cost_allocation_tags" {
  description = "Activate project/env cost allocation tags. Turn on after the first session, once the tags appear in billing data."
  type        = bool
  default     = false
}

variable "flink_writes_iceberg" {
  description = "Grant Flink Glue and iceberg/* access. Only for the fallback where Flink SQL writes Iceberg instead of the Kafka Connect sink."
  type        = bool
  default     = false
}

variable "eks_log_retention_days" {
  description = "Retention of the EKS control plane log group (kept in this layer so logs outlive each cluster)."
  type        = number
  default     = 7
}
