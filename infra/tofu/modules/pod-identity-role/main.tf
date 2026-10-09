# One IAM role per Kubernetes service account, assumable only through EKS Pod Identity.
# The trust policy pins the session tags that the Pod Identity agent sets, so an association
# pointing another namespace/service account at this role still cannot assume it.
resource "aws_iam_role" "this" {
  name                 = var.name
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EksPodIdentity"
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:SourceAccount"                         = var.account_id
          "aws:RequestTag/kubernetes-namespace"       = var.namespace
          "aws:RequestTag/kubernetes-service-account" = var.service_account
        }
        ArnEquals = { "aws:SourceArn" = var.cluster_arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "this" {
  count  = var.policy_json == null ? 0 : 1
  name   = "${var.name}-access"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

resource "aws_iam_role_policy_attachment" "managed" {
  for_each   = toset(var.managed_policy_arns)
  role       = aws_iam_role.this.name
  policy_arn = each.value
}
