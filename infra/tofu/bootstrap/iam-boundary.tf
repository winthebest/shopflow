# Permissions boundary on every shopflow role: the most any role can ever do, whatever its own
# policies say. It keeps work in one region, forbids long-lived credentials, and protects the
# guardrails (boundary, budgets, kill switch) from the roles they constrain.

locals {
  # Allowed in every region: global services (their API lives in us-east-1; the LB controller reads
  # the Shield subscription state), plus read-only calls the orphan check makes in all regions to
  # find forgotten capacity.
  region_exempt_actions = [
    "iam:*", "sts:*", "organizations:Describe*", "organizations:List*",
    "budgets:*", "ce:*", "cur:*", "health:*", "pricing:*", "support:*",
    "route53:*", "cloudfront:*", "shield:Get*", "shield:Describe*", "shield:List*",
    "s3:ListAllMyBuckets", "s3:GetAccountPublicAccessBlock",
    "ec2:Describe*", "elasticloadbalancing:Describe*", "eks:Describe*", "eks:List*", "tag:GetResources",
  ]

  boundary_arn       = "${local.arn.policy}/${local.boundary_name}"
  deny_policy_arn    = "${local.arn.policy}/${local.deny_policy_name}"
  budget_action_role = "${local.arn.role}/${local.roles.budget_action}"
  schedule_group_arn = "arn:aws:scheduler:${local.region}:${local.account_id}:schedule-group/${local.contract.reaper.schedule_group}"
  guardrail_api_denies = [
    "budgets:ModifyBudget", "budgets:DeleteBudgetAction", "budgets:UpdateBudgetAction", "budgets:ExecuteBudgetAction",
    "ce:DeleteAnomalyMonitor", "ce:UpdateAnomalyMonitor", "ce:DeleteAnomalySubscription", "ce:UpdateAnomalySubscription",
    "cloudtrail:StopLogging", "cloudtrail:DeleteTrail",
  ]
}

resource "aws_iam_policy" "boundary" {
  name        = local.boundary_name
  description = "Permissions boundary for every ${local.project} role."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowWithinBoundary"
        Effect   = "Allow"
        Action   = "*"
        Resource = "*"
      },
      {
        Sid       = "DenyOtherRegions"
        Effect    = "Deny"
        NotAction = local.region_exempt_actions
        Resource  = "*"
        Condition = { StringNotEquals = { "aws:RequestedRegion" = [local.region] } }
      },
      {
        Sid      = "DenyLongLivedCredentials"
        Effect   = "Deny"
        Action   = ["iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile"]
        Resource = "*"
      },
      {
        Sid      = "DenyBoundaryChanges"
        Effect   = "Deny"
        Action   = ["iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion"]
        Resource = local.boundary_arn
      },
      {
        Sid      = "DenyBoundaryRemoval"
        Effect   = "Deny"
        Action   = ["iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary"]
        Resource = "*"
      },
      {
        Sid       = "DenyRolesWithoutBoundary"
        Effect    = "Deny"
        Action    = "iam:CreateRole"
        Resource  = "*"
        Condition = { StringNotEquals = { "iam:PermissionsBoundary" = local.boundary_arn } }
      },
      {
        Sid      = "DenyGuardrailChanges"
        Effect   = "Deny"
        Action   = local.guardrail_api_denies
        Resource = "*"
      },
      {
        Sid    = "ProtectBackupReaper"
        Effect = "Deny"
        Action = [
          "lambda:DeleteFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
        ]
        Resource = local.arn.reaper_fn
      },
      {
        Sid      = "ProtectReaperScheduleGroup"
        Effect   = "Deny"
        Action   = "scheduler:DeleteScheduleGroup"
        Resource = local.schedule_group_arn
      },
      {
        Sid      = "OnlyBudgetActionDetachesItsPolicy"
        Effect   = "Deny"
        Action   = "iam:DetachRolePolicy"
        Resource = "*"
        Condition = {
          ArnEquals    = { "iam:PolicyARN" = local.deny_policy_arn }
          ArnNotEquals = { "aws:PrincipalArn" = local.budget_action_role }
        }
      },
    ]
  })
}
