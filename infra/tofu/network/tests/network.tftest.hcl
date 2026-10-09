mock_provider "aws" {}

variables {
  aws_account_id = "123456789012"
}

run "public_subnets_without_nat" {
  command = plan

  assert {
    condition     = length(aws_subnet.public) == 2 && length(distinct([for s in aws_subnet.public : s.availability_zone])) == 2
    error_message = "Two public subnets in two AZs (EKS control plane requirement)."
  }

  assert {
    condition     = alltrue([for s in aws_subnet.public : s.map_public_ip_on_launch])
    error_message = "Without NAT, nodes need public IPs for egress."
  }

  assert {
    condition     = aws_route.internet.destination_cidr_block == "0.0.0.0/0" && aws_route.internet.nat_gateway_id == null
    error_message = "The default route goes to the internet gateway, never a NAT gateway."
  }

  assert {
    condition     = [for s in aws_subnet.public : s.tags["shopflow.io/node-subnet"]] == ["true", "false"]
    error_message = "Only the AZ-a subnet hosts nodes."
  }

  assert {
    condition     = alltrue([for s in aws_subnet.public : s.tags["kubernetes.io/role/elb"] == "1"])
    error_message = "Subnets are tagged for internet-facing load balancers."
  }
}

run "default_security_group_is_emptied" {
  command = plan

  assert {
    condition     = length(aws_default_security_group.this.ingress) == 0 && length(aws_default_security_group.this.egress) == 0
    error_message = "The default security group has no rules."
  }
}

run "rejects_single_az" {
  command = plan

  variables {
    public_subnets = { a = "10.60.0.0/19" }
  }

  expect_failures = [var.public_subnets]
}
