resource "aws_launch_template" "node" {
  name_prefix            = "${local.cluster}-node-"
  update_default_version = true

  # IMDSv2 with hop limit 1: pods (one hop further than the node) cannot reach instance metadata,
  # so they cannot borrow the node role. Components that used IMDS for region/VPC get them
  # explicitly instead (e.g. LB controller --aws-region/--aws-vpc-id).
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = var.node_disk_gib
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # default_tags do not reach instances launched by the node group; these do, so cost allocation
  # and the reaper's project=shopflow conditions cover nodes, their disks and network interfaces.
  dynamic "tag_specifications" {
    for_each = toset(["instance", "volume", "network-interface"])
    content {
      resource_type = tag_specifications.value
      tags          = local.instance_tags
    }
  }
}

resource "aws_eks_node_group" "spot" {
  cluster_name           = aws_eks_cluster.this.name
  node_group_name_prefix = "${local.cluster}-spot-"
  node_role_arn          = data.aws_iam_role.node.arn
  # One AZ on purpose: EBS volumes are zonal, and multi-AZ HA is a non-goal (ADR 0504).
  subnet_ids     = [data.aws_subnet.node.id]
  capacity_type  = "SPOT"
  ami_type       = "AL2023_ARM_64_STANDARD"
  instance_types = var.node_instance_types
  labels         = { "shopflow.io/capacity" = "spot" }

  scaling_config {
    min_size     = 0
    desired_size = var.node_desired_size
    max_size     = var.node_max_size
  }

  update_config {
    max_unavailable = 1
  }

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  lifecycle {
    create_before_destroy = true
    # cloud-pause / cloud-resume scale the group outside OpenTofu.
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_eks_addon.before_nodes]
}

resource "terraform_data" "instance_type_allow_list" {
  lifecycle {
    precondition {
      condition     = alltrue([for t in var.node_instance_types : contains(local.contract.allowed_instance_types, t)])
      error_message = "node_instance_types must come from cloud-contract.json allowed_instance_types (the operator's RunInstances allow-list)."
    }
  }
}
