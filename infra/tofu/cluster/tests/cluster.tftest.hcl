# Offline checks of the session layer with a mocked AWS provider: no credentials, no API calls.
mock_provider "aws" {
  mock_data "aws_vpc" {
    defaults = { id = "vpc-0123456789abcdef0" }
  }
  mock_data "aws_subnets" {
    defaults = { ids = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"] }
  }
  mock_data "aws_subnet" {
    defaults = { id = "subnet-0aaaaaaaaaaaaaaaa" }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_data "aws_eks_addon_version" {
    defaults = { version = "v1.0.0-eksbuild.1" }
  }
  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0123456789abcdef0", latest_version = 1 }
  }
  mock_resource "aws_eks_cluster" {
    defaults = { arn = "arn:aws:eks:ap-southeast-1:123456789012:cluster/shopflow" }
  }
}

variables {
  aws_account_id = "123456789012"
  operator_cidr  = "203.0.113.10/32"
}

run "api_endpoint_only_for_the_operator" {
  command = plan

  assert {
    condition     = one(aws_eks_cluster.this.vpc_config).public_access_cidrs == toset(["203.0.113.10/32"])
    error_message = "The public API endpoint accepts only the operator's CIDR."
  }

  assert {
    condition     = one(aws_eks_cluster.this.upgrade_policy).support_type == "STANDARD"
    error_message = "The cluster must never fall into extended support ($0.60/h)."
  }

  assert {
    condition     = one(aws_eks_cluster.this.access_config).authentication_mode == "API" && !one(aws_eks_cluster.this.access_config).bootstrap_cluster_creator_admin_permissions
    error_message = "Access entries only; admin comes from the operator access entry, not from whoever created the cluster."
  }

  assert {
    condition     = aws_eks_access_policy_association.operator_admin.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
    error_message = "The operator role is cluster admin."
  }
}

run "nodes_are_graviton_spot_in_one_az_without_pod_imds" {
  command = plan

  assert {
    condition     = aws_eks_node_group.spot.capacity_type == "SPOT" && aws_eks_node_group.spot.ami_type == "AL2023_ARM_64_STANDARD"
    error_message = "Nodes are Graviton spot."
  }

  assert {
    condition     = aws_eks_node_group.spot.subnet_ids == toset(["subnet-0aaaaaaaaaaaaaaaa"])
    error_message = "Nodes live only in the AZ-a subnet."
  }

  assert {
    condition     = one(aws_eks_node_group.spot.scaling_config).min_size == 0
    error_message = "The node group can scale to zero (cloud-pause)."
  }

  assert {
    condition     = one(aws_launch_template.node.metadata_options).http_tokens == "required" && one(aws_launch_template.node.metadata_options).http_put_response_hop_limit == 1
    error_message = "IMDSv2 with hop limit 1 keeps node credentials away from pods."
  }

  assert {
    condition = toset([for t in aws_launch_template.node.tag_specifications : t.resource_type]) == toset(["instance", "volume", "network-interface"]) && alltrue([
      for t in aws_launch_template.node.tag_specifications : t.tags["project"] == "shopflow" && t.tags["env"] == "aws"
    ])
    error_message = "Instances, volumes and ENIs launched by the node group carry project/env tags."
  }
}

run "addons_and_pod_identity_follow_the_contract" {
  command = plan

  assert {
    condition     = toset(concat(keys(aws_eks_addon.before_nodes), keys(aws_eks_addon.after_nodes))) == toset(["vpc-cni", "kube-proxy", "eks-pod-identity-agent", "coredns", "aws-ebs-csi-driver"])
    error_message = "The five managed addons are installed."
  }

  assert {
    condition     = jsondecode(aws_eks_addon.before_nodes["vpc-cni"].configuration_values).enableNetworkPolicy == "true"
    error_message = "vpc-cni enforces NetworkPolicy."
  }

  assert {
    condition     = jsondecode(aws_eks_addon.after_nodes["aws-ebs-csi-driver"].configuration_values).controller.extraVolumeTags == { project = "shopflow", env = "aws" }
    error_message = "EBS CSI tags every volume it creates."
  }

  assert {
    condition = {
      for k, a in aws_eks_pod_identity_association.this : k => "${a.namespace}/${a.service_account}"
      } == {
      for k, v in jsondecode(file("../../cloud-contract.json")).pod_identities : k => "${v.namespace}/${v.service_account}"
    }
    error_message = "Pod Identity associations match cloud-contract.json."
  }
}

run "addon_version_can_be_pinned" {
  command = plan

  variables {
    addon_versions = { coredns = "v1.11.4-eksbuild.2" }
  }

  assert {
    condition     = aws_eks_addon.after_nodes["coredns"].addon_version == "v1.11.4-eksbuild.2" && !contains(keys(data.aws_eks_addon_version.this), "coredns")
    error_message = "A pinned version replaces the default lookup."
  }
}

run "rejects_open_api_endpoint" {
  command = plan

  variables {
    operator_cidr = "0.0.0.0/0"
  }

  expect_failures = [var.operator_cidr]
}

run "rejects_x86_instance_types" {
  command = plan

  variables {
    node_instance_types = ["m7i.xlarge", "m6i.xlarge"]
  }

  expect_failures = [var.node_instance_types]
}

run "rejects_types_outside_the_operator_allow_list" {
  command = plan

  variables {
    node_instance_types = ["m7g.8xlarge", "m7g.xlarge"]
  }

  expect_failures = [terraform_data.instance_type_allow_list]
}
