# shopflow-operator: the role a human assumes (IAM Identity Center -> role chaining) to run
# cloud-up/down and apply the network and cluster layers. It cannot change IAM, so layer 0 itself
# is applied with the Identity Center admin permission set, never with this role.

locals {
  operator_state_write_keys = [
    "${local.arn.state_bucket}/${local.contract.state_keys.network}*",
    "${local.arn.state_bucket}/${local.contract.state_keys.cluster}*",
  ]

  operator_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SessionCompute"
        Effect   = "Allow"
        Action   = ["eks:*", "ec2:*", "elasticloadbalancing:*", "autoscaling:Describe*"]
        Resource = "*"
      },
      {
        Sid    = "ReadAccount"
        Effect = "Allow"
        Action = [
          "iam:Get*", "iam:List*", "sts:GetCallerIdentity",
          "budgets:ViewBudget", "budgets:DescribeBudgetAction*", "ce:Get*", "ce:Describe*", "ce:List*",
          "logs:Describe*", "logs:Get*", "logs:FilterLogEvents", "logs:StartQuery", "logs:StopQuery",
          "cloudwatch:Describe*", "cloudwatch:Get*", "cloudwatch:List*",
          "glue:GetDatabase*", "glue:GetTable*", "tag:GetResources", "servicequotas:Get*", "servicequotas:List*",
          "pricing:GetProducts", "ssm:DescribeParameters", "s3:ListAllMyBuckets",
          "lambda:GetFunction", "scheduler:GetScheduleGroup", "scheduler:ListSchedules", "sns:GetTopicAttributes",
        ]
        Resource = "*"
      },
      {
        Sid      = "PassSessionRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = ["${local.arn.role}/${local.roles.cluster}", "${local.arn.role}/${local.roles.node}", "${local.arn.role}/${local.roles.pod_prefix}*"]
      },
      {
        Sid       = "PassSchedulerRole"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = "${local.arn.role}/${local.roles.reaper_scheduler}"
        Condition = { StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" } }
      },
      {
        Sid      = "FirstSessionServiceLinkedRoles"
        Effect   = "Allow"
        Action   = "iam:CreateServiceLinkedRole"
        Resource = "*"
        Condition = {
          StringEquals = {
            "iam:AWSServiceName" = [
              "eks.amazonaws.com", "eks-nodegroup.amazonaws.com", "autoscaling.amazonaws.com",
              "spot.amazonaws.com", "elasticloadbalancing.amazonaws.com",
            ]
          }
        }
      },
      {
        Sid      = "SessionParameters"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:PutParameter", "ssm:DeleteParameter", "ssm:DeleteParameters", "ssm:AddTagsToResource", "ssm:ListTagsForResource"]
        Resource = "${local.arn.parameter}${local.contract.ssm.prefix}/*"
      },
      {
        Sid       = "SsmDefaultKey"
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "ssm.${local.region}.amazonaws.com" } }
      },
      {
        Sid      = "ReadState"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetObject"]
        Resource = [local.arn.state_bucket, "${local.arn.state_bucket}/*"]
      },
      {
        Sid      = "WriteNetworkAndClusterState"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject"]
        Resource = local.operator_state_write_keys
      },
      {
        Sid      = "ReadDataBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject"]
        Resource = [local.arn.data_bucket, "${local.arn.data_bucket}/*"]
      },
      {
        Sid      = "UploadEvidence"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${local.arn.data_bucket}/${local.prefixes.evidence}*"
      },
      {
        Sid      = "ReaperSchedule"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"]
        Resource = local.arn.schedules
      },
      {
        Sid      = "TestBackupReaper"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.arn.reaper_fn
      },
      {
        Sid      = "PublishAlerts"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = module.cost_guardrails.alert_topic_arn
      },
      {
        Sid      = "DenyDirectInstancesOutsideAllowList"
        Effect   = "Deny"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          StringNotEquals = { "ec2:InstanceType" = local.contract.allowed_instance_types }
        }
      },
      {
        Sid       = "DenyDirectInstancesWithoutProjectTag"
        Effect    = "Deny"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringNotEquals = { "aws:RequestTag/project" = local.project } }
      },
      {
        Sid    = "DenyCostTraps"
        Effect = "Deny"
        Action = [
          "ec2:CreateNatGateway", "ec2:AllocateAddress", "ec2:AllocateHosts", "ec2:CreateCapacityReservation",
          "ec2:PurchaseReservedInstancesOffering", "ec2:PurchaseHostReservation", "ec2:PurchaseScheduledInstances",
          "eks:CreateFargateProfile",
        ]
        Resource = "*"
      },
    ]
  }
}

resource "aws_iam_role" "operator" {
  name                 = local.roles.operator
  description          = "Human operator for shopflow cloud sessions (assumed from IAM Identity Center)."
  permissions_boundary = aws_iam_policy.boundary.arn
  # Role chaining from Identity Center caps sessions at one hour regardless of this value; the AWS
  # CLI refreshes chained credentials automatically.
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "IdentityCenterOperator"
      Effect    = "Allow"
      Principal = { AWS = local.arn.account }
      Action    = ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"]
      Condition = { ArnLike = { "aws:PrincipalArn" = local.operator_principal_arns } }
    }]
  })
}

resource "aws_iam_policy" "operator" {
  name        = local.roles.operator
  description = "Session permissions for ${local.roles.operator}."
  policy      = jsonencode(local.operator_policy)
}

resource "aws_iam_role_policy_attachment" "operator" {
  role       = aws_iam_role.operator.name
  policy_arn = aws_iam_policy.operator.arn
}
