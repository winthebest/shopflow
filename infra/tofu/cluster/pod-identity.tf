# Namespace/service account -> role, from cloud-contract.json (the contract with the Helm charts,
# documented in docs/runbooks/cloud-session.md). The roles themselves live in layer 0.
resource "aws_eks_pod_identity_association" "this" {
  for_each        = local.contract.pod_identities
  cluster_name    = aws_eks_cluster.this.name
  namespace       = each.value.namespace
  service_account = each.value.service_account
  role_arn        = data.aws_iam_role.pod[each.key].arn

  depends_on = [aws_eks_addon.before_nodes]
}
