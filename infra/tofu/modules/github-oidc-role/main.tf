locals {
  aud_claim = "token.actions.githubusercontent.com:aud"
  sub_claim = "token.actions.githubusercontent.com:sub"

  # Audience is always matched exactly; the subject goes under StringEquals or StringLike.
  trust_condition = {
    for operator, claims in {
      StringEquals = merge(
        { (local.aud_claim) = ["sts.amazonaws.com"] },
        var.subject_match == "StringEquals" ? { (local.sub_claim) = var.subjects } : {},
      )
      StringLike = var.subject_match == "StringLike" ? { (local.sub_claim) = var.subjects } : {}
    } : operator => claims if length(claims) > 0
  }
}

# Role assumable only by specific GitHub Actions workflows through OIDC (no long-lived keys).
resource "aws_iam_role" "this" {
  name                 = var.name
  permissions_boundary = var.permissions_boundary_arn
  max_session_duration = var.max_session_duration
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GitHubActionsOidc"
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = local.trust_condition
    }]
  })
}

resource "aws_iam_role_policy" "this" {
  name   = "${var.name}-access"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}
