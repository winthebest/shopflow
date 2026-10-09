locals {
  # Installed before nodes join: with bootstrap_self_managed_addons = false nothing else gives the
  # nodes networking. vpc-cni also enforces NetworkPolicy and tags the ENIs it creates.
  addons_before_nodes = {
    "vpc-cni" = jsonencode({
      enableNetworkPolicy = "true"
      env = {
        ADDITIONAL_ENI_TAGS = jsonencode({ project = local.project, env = local.env })
      }
    })
    "kube-proxy"             = null
    "eks-pod-identity-agent" = null
  }

  # Need running nodes to become healthy.
  addons_after_nodes = {
    "coredns" = null
    "aws-ebs-csi-driver" = jsonencode({
      controller = {
        extraVolumeTags = { project = local.project, env = local.env }
      }
    })
  }
}

data "aws_eks_addon_version" "this" {
  for_each           = setsubtract(toset(concat(keys(local.addons_before_nodes), keys(local.addons_after_nodes))), toset(keys(var.addon_versions)))
  addon_name         = each.value
  kubernetes_version = local.kubernetes_version
  most_recent        = false
}

resource "aws_eks_addon" "before_nodes" {
  for_each                    = local.addons_before_nodes
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = lookup(var.addon_versions, each.key, try(data.aws_eks_addon_version.this[each.key].version, null))
  configuration_values        = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_addon" "after_nodes" {
  for_each                    = local.addons_after_nodes
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = lookup(var.addon_versions, each.key, try(data.aws_eks_addon_version.this[each.key].version, null))
  configuration_values        = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.spot, aws_eks_pod_identity_association.this]
}
