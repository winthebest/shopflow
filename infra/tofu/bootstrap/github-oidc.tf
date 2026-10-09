# GitHub Actions roles. Neither can apply infrastructure: ci-plan only reads, the reaper only
# deletes project-tagged session compute. cloud-up/down run from the operator's laptop.
#
# The repository customizes the OIDC `sub` claim to include job_workflow_ref (repo setting, see
# docs/runbooks/cloud-session.md), so each role trusts one workflow file, not the whole repo.

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  gh_repo = local.github_repository

  ci_plan_subject = "repo:${local.gh_repo}:pull_request:job_workflow_ref:${local.gh_repo}/${local.workflows.ci_plan}@refs/pull/*/merge"
  reaper_subject  = "repo:${local.gh_repo}:ref:refs/heads/main:job_workflow_ref:${local.gh_repo}/${local.workflows.reaper}@refs/heads/main"

  cluster_state_objects = [
    "${local.arn.state_bucket}/${local.contract.state_keys.cluster}",
    "${local.arn.state_bucket}/${local.contract.state_keys.cluster}.tflock",
  ]

  ci_plan_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadState"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetObject"]
        Resource = [local.arn.state_bucket, "${local.arn.state_bucket}/*"]
      },
      {
        Sid    = "DescribeInfrastructure"
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity", "ec2:Describe*", "eks:Describe*", "eks:List*", "iam:Get*", "iam:List*",
          "s3:GetBucket*", "s3:GetAccelerateConfiguration", "s3:GetLifecycleConfiguration", "s3:GetEncryptionConfiguration",
          "s3:GetReplicationConfiguration", "s3:ListBucket",
          "glue:GetDatabase", "glue:GetDatabases", "glue:GetTags",
          "lambda:GetFunction", "lambda:GetFunctionCodeSigningConfig", "lambda:ListVersionsByFunction", "lambda:GetPolicy", "lambda:ListTags",
          "logs:DescribeLogGroups", "logs:ListTagsForResource", "logs:ListTagsLogGroup",
          "cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource",
          "sns:GetTopicAttributes", "sns:GetSubscriptionAttributes", "sns:ListSubscriptionsByTopic", "sns:ListTagsForResource",
          "budgets:ViewBudget", "budgets:DescribeBudgetAction*", "budgets:ListTagsForResource",
          "ce:GetAnomalyMonitors", "ce:GetAnomalySubscriptions", "ce:ListCostAllocationTags", "ce:ListTagsForResource",
          "scheduler:GetScheduleGroup", "scheduler:ListTagsForResource",
        ]
        Resource = "*"
      },
      {
        Sid      = "NeverReadSecrets"
        Effect   = "Deny"
        Action   = ["ssm:GetParameter*", "kms:Decrypt", "secretsmanager:GetSecretValue"]
        Resource = "*"
      },
      {
        Sid      = "NeverTouchDataObjects"
        Effect   = "Deny"
        Action   = ["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject"]
        Resource = "${local.arn.data_bucket}/*"
      },
      {
        Sid      = "NeverWriteState"
        Effect   = "Deny"
        Action   = ["s3:PutObject*", "s3:DeleteObject*"]
        Resource = "${local.arn.state_bucket}/*"
      },
    ]
  }

  reaper_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListState"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = local.arn.state_bucket
      },
      {
        Sid      = "LockAndWriteClusterStateOnly"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = local.cluster_state_objects
      },
      {
        Sid    = "Discover"
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity", "ec2:Describe*", "eks:Describe*", "eks:List*", "elasticloadbalancing:Describe*",
          "autoscaling:Describe*", "tag:GetResources", "iam:GetRole",
        ]
        Resource = "*"
      },
      {
        Sid      = "ReadLease"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "${local.arn.parameter}${local.contract.ssm.lease}"
      },
      {
        Sid    = "DeleteTaggedSession"
        Effect = "Allow"
        Action = [
          "eks:DeleteCluster", "eks:DeleteNodegroup", "eks:DeleteAddon", "eks:DeletePodIdentityAssociation",
          "eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy",
          "ec2:DeleteLaunchTemplate", "ec2:DeleteVolume", "ec2:DeleteNetworkInterface", "ec2:DeleteSecurityGroup",
          "ec2:TerminateInstances", "ec2:ReleaseAddress", "ec2:DeleteNatGateway",
          "elasticloadbalancing:DeleteLoadBalancer", "elasticloadbalancing:DeleteTargetGroup",
        ]
        Resource  = "*"
        Condition = { StringEquals = { "aws:ResourceTag/project" = local.project } }
      },
      {
        Sid      = "NeverChangeIam"
        Effect   = "Deny"
        Action   = ["iam:Create*", "iam:Attach*", "iam:Put*", "iam:Update*", "iam:Delete*", "iam:Detach*", "iam:PassRole"]
        Resource = "*"
      },
      {
        Sid      = "NeverTouchLayer0State"
        Effect   = "Deny"
        Action   = ["s3:PutObject*", "s3:DeleteObject*"]
        Resource = ["${local.arn.state_bucket}/${local.contract.state_keys.bootstrap}*", "${local.arn.state_bucket}/${local.contract.state_keys.network}*"]
      },
      {
        Sid      = "NeverTouchData"
        Effect   = "Deny"
        Action   = "s3:*"
        Resource = [local.arn.data_bucket, "${local.arn.data_bucket}/*"]
      },
      {
        Sid    = "NeverTouchLayer0Services"
        Effect = "Deny"
        Action = [
          "ssm:PutParameter", "ssm:DeleteParameter*", "glue:Delete*", "glue:Update*", "lambda:*", "budgets:Modify*",
          "budgets:Delete*", "sns:Delete*", "logs:Delete*", "scheduler:Delete*", "kms:*",
        ]
        Resource = "*"
      },
    ]
  }
}

module "ci_plan_role" {
  source = "../modules/github-oidc-role"

  name                     = local.roles.ci_plan
  oidc_provider_arn        = aws_iam_openid_connect_provider.github.arn
  subjects                 = [local.ci_plan_subject]
  subject_match            = "StringLike"
  permissions_boundary_arn = aws_iam_policy.boundary.arn
  policy_json              = jsonencode(local.ci_plan_policy)
}

module "reaper_role" {
  source = "../modules/github-oidc-role"

  name                     = local.roles.reaper
  oidc_provider_arn        = aws_iam_openid_connect_provider.github.arn
  subjects                 = [local.reaper_subject]
  permissions_boundary_arn = aws_iam_policy.boundary.arn
  policy_json              = jsonencode(local.reaper_policy)
}
