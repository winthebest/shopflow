variable "name" {
  description = "IAM role name."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace of the service account allowed to assume the role."
  type        = string
}

variable "service_account" {
  description = "Kubernetes service account allowed to assume the role."
  type        = string
}

variable "cluster_arn" {
  description = "ARN of the EKS cluster whose Pod Identity agent may assume the role. Stable across cluster re-creation because the name is fixed."
  type        = string
}

variable "account_id" {
  description = "AWS account that owns the cluster (aws:SourceAccount)."
  type        = string
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary attached to the role."
  type        = string
}

variable "policy_json" {
  description = "Inline policy document (JSON). Null when the role only uses managed policies."
  type        = string
  default     = null
}

variable "managed_policy_arns" {
  description = "Managed policies to attach."
  type        = list(string)
  default     = []
}
