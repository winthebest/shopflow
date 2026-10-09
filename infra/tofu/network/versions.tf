terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.65.0"
    }
  }

  # Bucket passed at init: -backend-config=bucket=shopflow-tfstate-<account>.
  backend "s3" {
    key          = "network/terraform.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
