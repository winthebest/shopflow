variable "aws_account_id" {
  description = "Account this layer may touch; the provider refuses any other credentials."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "vpc_cidr" {
  description = "CIDR of the shopflow VPC."
  type        = string
  default     = "10.60.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be an IPv4 CIDR block."
  }
}

variable "public_subnets" {
  description = "Public subnets by AZ suffix. EKS needs two AZs for its control plane; nodes use only contract.node_az."
  type        = map(string)
  default = {
    a = "10.60.0.0/19"
    b = "10.60.32.0/19"
  }

  validation {
    condition     = length(var.public_subnets) >= 2
    error_message = "The EKS control plane needs subnets in at least two AZs."
  }
}
