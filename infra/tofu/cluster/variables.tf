variable "aws_account_id" {
  description = "Account this layer may touch; the provider refuses any other credentials."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "operator_cidr" {
  description = "Only source allowed to reach the public EKS API endpoint. cloud-up passes the operator's current public IP as /32."
  type        = string

  validation {
    condition     = can(cidrhost(var.operator_cidr, 0)) && tonumber(split("/", var.operator_cidr)[1]) >= 24
    error_message = "operator_cidr must be an IPv4 CIDR no wider than /24 (normally the operator's /32)."
  }
}

variable "kubernetes_version" {
  description = "EKS version override; null uses cloud-contract.json. Must be in standard support ($0.10/h, extended costs $0.60/h); cloud-up checks it before applying."
  type        = string
  default     = null

  validation {
    condition     = var.kubernetes_version == null || can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "kubernetes_version looks like 1.35."
  }
}

variable "node_instance_types" {
  description = "Graviton spot instance types override; null uses node_instance_types from cloud-contract.json (ADR 0505)."
  type        = list(string)
  default     = null

  validation {
    condition     = var.node_instance_types == null || (length(coalesce(var.node_instance_types, [])) >= 2 && alltrue([for t in coalesce(var.node_instance_types, []) : can(regex("^[a-z][0-9]+g[a-z]*\\.", t))]))
    error_message = "Use at least two Graviton (arm64) instance types."
  }
}

variable "node_desired_size" {
  description = "Nodes started by cloud-up. cloud-pause/resume change it outside OpenTofu (ignored in plans)."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Upper bound of the node group."
  type        = number
  default     = 3
}

variable "node_disk_gib" {
  description = "Root volume size of each node."
  type        = number
  default     = 50
}

variable "enabled_cluster_log_types" {
  description = "Control plane log types sent to the layer-0 log group."
  type        = list(string)
  default     = ["audit", "authenticator"]
}

variable "addon_versions" {
  description = "Pin an addon to a version; addons not listed use the default version for kubernetes_version."
  type        = map(string)
  default     = {}
}
