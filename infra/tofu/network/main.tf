# Layer 1: free networking kept between sessions. Public subnets with an internet gateway and no
# NAT gateway (~$0.06/h + data); nodes get public IPs, and only the EKS cluster security group
# (members only) and LB-controller-managed rules let traffic in.

locals {
  contract = jsondecode(file("${path.module}/../../cloud-contract.json"))
  project  = local.contract.project
  region   = local.contract.region
  cluster  = local.contract.cluster_name
  node_az  = local.contract.node_az
}

#trivy:ignore:AWS-0178 No flow logs: they need a log destination and role for a VPC that holds only short-lived sessions; NetworkPolicy and SGs are the controls (ADR 0512).
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.project }
}

# The default security group allows all traffic between its members; nothing should use it.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.project}-default-unused" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = local.project }
}

#trivy:ignore:AWS-0164 Public subnets are the no-NAT design: nodes need public IPs for egress (ADR 0503).
resource "aws_subnet" "public" {
  for_each                = var.public_subnets
  vpc_id                  = aws_vpc.this.id
  cidr_block              = each.value
  availability_zone       = "${local.region}${each.key}"
  map_public_ip_on_launch = true

  tags = {
    Name                                     = "${local.project}-public-${each.key}"
    "shopflow.io/tier"                       = "public"
    "shopflow.io/node-subnet"                = tostring("${local.region}${each.key}" == local.node_az)
    "kubernetes.io/role/elb"                 = "1"
    "kubernetes.io/cluster/${local.cluster}" = "shared"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.project}-public" }
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each       = var.public_subnets
  subnet_id      = aws_subnet.public[each.key].id
  route_table_id = aws_route_table.public.id
}

resource "terraform_data" "node_az_has_subnet" {
  lifecycle {
    precondition {
      condition     = contains([for k, _ in var.public_subnets : "${local.region}${k}"], local.node_az)
      error_message = "contract.node_az must be one of the public subnet AZs."
    }
  }
}
