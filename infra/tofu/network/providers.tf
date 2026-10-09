provider "aws" {
  region              = local.region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      project    = local.project
      env        = local.contract.env
      layer      = "network"
      managed-by = "opentofu"
    }
  }
}
