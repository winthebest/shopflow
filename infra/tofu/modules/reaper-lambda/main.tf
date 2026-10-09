# Backup kill switch that does not depend on GitHub: EventBridge Scheduler invokes this function
# from lease + grace until the session's billable resources are gone. AWS APIs only, so it works
# when the node group is scaled to zero or the cluster endpoint is unreachable.

locals {
  cluster_arn     = "arn:aws:eks:${var.region}:${var.account_id}:cluster/${var.cluster_name}"
  nodegroup_arns  = "arn:aws:eks:${var.region}:${var.account_id}:nodegroup/${var.cluster_name}/*/*"
  schedule_arn    = "arn:aws:scheduler:${var.region}:${var.account_id}:schedule/${var.schedule_group_name}/${var.schedule_name}"
  lease_param_arn = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${var.lease_parameter_name}"
  project_tag     = { "aws:ResourceTag/project" = var.project }
}

data "archive_file" "package" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.module}/.package/${var.function_name}.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

#trivy:ignore:AWS-0017 Log group uses the default CloudWatch encryption; a customer key costs $1/month and logs hold no secrets.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "lambda" {
  name                 = var.role_name
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.role_name}-access"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.this.arn}:*"
      },
      {
        Sid      = "ReadLease"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = local.lease_param_arn
      },
      {
        Sid    = "Discover"
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster", "eks:ListNodegroups", "eks:DescribeNodegroup",
          "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTags",
          "ec2:DescribeVolumes", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSecurityGroups",
        ]
        Resource = "*"
      },
      {
        Sid       = "DeleteEksSession"
        Effect    = "Allow"
        Action    = ["eks:DeleteNodegroup", "eks:DeleteCluster"]
        Resource  = [local.cluster_arn, local.nodegroup_arns]
        Condition = { StringEquals = local.project_tag }
      },
      {
        Sid       = "DeleteTaggedCapacity"
        Effect    = "Allow"
        Action    = ["elasticloadbalancing:DeleteLoadBalancer", "elasticloadbalancing:DeleteTargetGroup", "ec2:DeleteVolume", "ec2:DeleteNetworkInterface", "ec2:DeleteSecurityGroup"]
        Resource  = "*"
        Condition = { StringEquals = local.project_tag }
      },
      {
        Sid      = "RemoveOwnSchedule"
        Effect   = "Allow"
        Action   = "scheduler:DeleteSchedule"
        Resource = local.schedule_arn
      },
      {
        Sid      = "Notify"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = var.alert_topic_arn
      },
    ]
  })
}

#trivy:ignore:AWS-0066 X-Ray tracing adds cost and permissions for a function that runs a few times per month.
resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  description      = "Backup kill switch: tears down the ${var.cluster_name} EKS session once its lease has expired."
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "reaper.handler.lambda_handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  timeout          = 900
  memory_size      = 256

  environment {
    variables = {
      REAPER_CLUSTER_NAME    = var.cluster_name
      REAPER_PROJECT         = var.project
      REAPER_LEASE_PARAMETER = var.lease_parameter_name
      REAPER_SCHEDULE_GROUP  = var.schedule_group_name
      REAPER_SCHEDULE_NAME   = var.schedule_name
      REAPER_ALERT_TOPIC_ARN = var.alert_topic_arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy.lambda]
}

# A failed reaper is an alert, not a silent retry.
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.function_name}-errors"
  alarm_description   = "The backup reaper failed; the session may still be billing."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.this.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alert_topic_arn]
}

resource "aws_scheduler_schedule_group" "this" {
  name = var.schedule_group_name
}

resource "aws_iam_role" "scheduler" {
  name                 = var.scheduler_role_name
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.scheduler_role_name}-invoke"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.this.arn
    }]
  })
}
