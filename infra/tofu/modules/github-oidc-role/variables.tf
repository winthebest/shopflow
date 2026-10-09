variable "name" {
  description = "IAM role name."
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC identity provider (token.actions.githubusercontent.com)."
  type        = string
}

variable "subjects" {
  description = <<-EOT
    Allowed values of the `sub` claim. The repository customizes `sub` to
    `repo:<owner>/<repo>:<context>:job_workflow_ref:<owner>/<repo>/.github/workflows/<file>@<ref>`,
    so a subject names both the trigger context and the exact workflow file that may assume the role.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.subjects) > 0 && alltrue([for s in var.subjects : can(regex("^repo:[^:*]+/[^:*]+:.+:job_workflow_ref:[^*]+/\\.github/workflows/[^*]+\\.ya?ml@.+$", s))])
    error_message = "Each subject must pin the repository and a workflow file through job_workflow_ref; wildcards are allowed only in the ref."
  }
}

variable "subject_match" {
  description = "IAM condition operator for `sub`: StringEquals for an exact ref, StringLike when the ref contains `*` (pull request merge refs)."
  type        = string
  default     = "StringEquals"

  validation {
    condition     = contains(["StringEquals", "StringLike"], var.subject_match)
    error_message = "subject_match must be StringEquals or StringLike."
  }
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary attached to the role."
  type        = string
}

variable "policy_json" {
  description = "Inline policy document (JSON)."
  type        = string
}

variable "max_session_duration" {
  description = "Maximum session duration in seconds."
  type        = number
  default     = 3600
}
