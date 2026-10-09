terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.65.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }

  # Partial configuration: the bucket name contains the account ID and is passed at init
  # (`-backend-config=bucket=shopflow-tfstate-<account>`). The very first apply creates that bucket,
  # so it runs on local state through a gitignored `local_override.tf` and then migrates
  # (docs/runbooks/cloud-session.md, "Bootstrap layer 0").
  backend "s3" {
    key          = "bootstrap/terraform.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
