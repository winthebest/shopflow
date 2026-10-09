# Layer 2 looks up layers 0 and 1 by name and tag instead of reading their state: its own state
# then holds only AWS resources of the session, and the reaper needs access to this state alone.

locals {
  contract = jsondecode(file("${path.module}/../../cloud-contract.json"))
  project  = local.contract.project
  env      = local.contract.env
  region   = local.contract.region
  cluster  = local.contract.cluster_name
  roles    = local.contract.roles

  kubernetes_version  = coalesce(var.kubernetes_version, local.contract.kubernetes_version)
  node_instance_types = coalesce(var.node_instance_types, local.contract.node_instance_types)

  instance_tags = {
    project = local.project
    env     = local.env
    Name    = "${local.cluster}-node"
  }
}

data "aws_vpc" "this" {
  tags = {
    Name    = local.project
    project = local.project
  }
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.this.id]
  }

  tags = { "shopflow.io/tier" = "public" }
}

data "aws_subnet" "node" {
  vpc_id            = data.aws_vpc.this.id
  availability_zone = local.contract.node_az

  tags = { "shopflow.io/tier" = "public" }
}

data "aws_iam_role" "cluster" {
  name = local.roles.cluster
}

data "aws_iam_role" "node" {
  name = local.roles.node
}

data "aws_iam_role" "operator" {
  name = local.roles.operator
}

data "aws_iam_role" "pod" {
  for_each = local.contract.pod_identities
  name     = "${local.roles.pod_prefix}${each.key}"
}
