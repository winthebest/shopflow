#trivy:ignore:AWS-0040 The public endpoint is restricted to the operator's /32 (public_access_cidrs); a private-only endpoint would need a VPN or bastion.
#trivy:ignore:AWS-0041 Restricted by public_access_cidrs = [operator_cidr]; trivy cannot evaluate the variable.
#trivy:ignore:AWS-0039 EKS envelope-encrypts secrets by default with an AWS-owned key; a customer key costs $1/month.
#trivy:ignore:AWS-0038 audit + authenticator are on; api/controllerManager/scheduler logs are high-volume CloudWatch ingest for little value in short sessions.
resource "aws_eks_cluster" "this" {
  name                          = local.cluster
  version                       = local.kubernetes_version
  role_arn                      = data.aws_iam_role.cluster.arn
  enabled_cluster_log_types     = var.enabled_cluster_log_types
  bootstrap_self_managed_addons = false

  vpc_config {
    subnet_ids              = data.aws_subnets.public.ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.operator_cidr]
  }

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  # At the end of standard support EKS upgrades the cluster instead of moving it to extended
  # support ($0.60/h instead of $0.10/h).
  upgrade_policy {
    support_type = "STANDARD"
  }
}

resource "aws_eks_access_entry" "operator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_iam_role.operator.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "operator_admin" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.operator.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}
