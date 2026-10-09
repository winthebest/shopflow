variable "name_prefix" {
  description = "Prefix for budget, topic and anomaly resources."
  type        = string
}

variable "account_id" {
  description = "AWS account ID (used to scope service trust policies)."
  type        = string
}

variable "alert_email" {
  description = "Email that receives budget, anomaly and reaper alerts. Supplied at apply time, never committed."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary attached to the budget action execution role."
  type        = string
}

variable "budget_limit_usd" {
  description = "Budget limit for the whole budget period (the project credit cap)."
  type        = number
  default     = 100
}

variable "budget_time_unit" {
  description = "Budget period. CUSTOM covers the whole project window (budget_start..budget_end) without resetting, so the USD thresholds are cumulative; MONTHLY/QUARTERLY/ANNUALLY would reset them mid-project."
  type        = string
  default     = "CUSTOM"

  validation {
    condition     = contains(["MONTHLY", "QUARTERLY", "ANNUALLY", "CUSTOM"], var.budget_time_unit)
    error_message = "budget_time_unit must be MONTHLY, QUARTERLY, ANNUALLY or CUSTOM."
  }
}

variable "budget_start" {
  description = "Start of the budget window, format YYYY-MM-DD_hh:mm (UTC)."
  type        = string
  default     = "2026-10-01_00:00"
}

variable "budget_end" {
  description = "End of the budget window (UTC). AWS deletes the budget after this date, so keep it past the credit expiry (12 months after account creation once on the Paid plan)."
  type        = string
  default     = "2027-10-01_00:00"
}

variable "actual_alert_thresholds_usd" {
  description = "Actual (not forecast) spend, in USD, that triggers an email."
  type        = list(number)
  default     = [10, 20]
}

variable "forecast_alert_threshold_usd" {
  description = "Forecasted spend, in USD, that triggers an email."
  type        = number
  default     = 25
}

variable "action_threshold_usd" {
  description = "Actual spend, in USD, at which the Budget Action attaches the deny policy. A deliberate checkpoint: raise it only after reviewing docs/cost.md."
  type        = number
  default     = 25
}

variable "action_target_role_names" {
  description = "Roles that receive the deny-create policy when the Budget Action fires."
  type        = list(string)
}

variable "deny_create_actions" {
  description = "Actions denied by the Budget Action policy. Deletes, scale-down and reads stay allowed so a session can still be torn down."
  type        = list(string)
  default = [
    "eks:CreateCluster",
    "eks:CreateNodegroup",
    "ec2:RunInstances",
    "ec2:StartInstances",
    "ec2:CreateVolume",
    "ec2:CreateNatGateway",
    "ec2:AllocateAddress",
    "elasticloadbalancing:CreateLoadBalancer",
  ]
}

variable "anomaly_monitor_arn" {
  description = "Existing Cost Anomaly Detection monitor to subscribe to. New accounts get an AWS-managed services monitor and only one DIMENSIONAL/SERVICE monitor is allowed, so reuse it when present; null creates one."
  type        = string
  default     = null
}

variable "anomaly_threshold_usd" {
  description = "Minimum total impact, in USD, for an anomaly to be sent."
  type        = number
  default     = 1
}

variable "activate_cost_allocation_tags" {
  description = "Activate project/env as cost allocation tags. A tag key must exist on a billed resource (~24h after first use) before it can be activated, so this starts false and is turned on after the first session."
  type        = bool
  default     = false
}

variable "cost_allocation_tag_keys" {
  description = "Tag keys to activate for cost allocation."
  type        = list(string)
  default     = ["project", "env"]
}
