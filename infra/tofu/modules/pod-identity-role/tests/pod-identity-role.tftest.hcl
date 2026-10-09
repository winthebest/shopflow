mock_provider "aws" {}

variables {
  name                     = "shopflow-pod-cnpg"
  namespace                = "shop"
  service_account          = "shop-db"
  cluster_arn              = "arn:aws:eks:ap-southeast-1:123456789012:cluster/shopflow"
  account_id               = "123456789012"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/shopflow-boundary"
  policy_json              = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
}

run "trust_is_pinned_to_one_service_account" {
  command = apply

  assert {
    condition     = jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Principal.Service == "pods.eks.amazonaws.com"
    error_message = "Only the EKS Pod Identity service may assume the role."
  }

  assert {
    condition = jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Condition.StringEquals == {
      "aws:SourceAccount"                         = "123456789012"
      "aws:RequestTag/kubernetes-namespace"       = "shop"
      "aws:RequestTag/kubernetes-service-account" = "shop-db"
    }
    error_message = "Trust policy must pin account, namespace and service account."
  }

  assert {
    condition     = jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == var.cluster_arn
    error_message = "Trust policy must pin the cluster ARN."
  }

  assert {
    condition     = aws_iam_role.this.permissions_boundary == var.permissions_boundary_arn
    error_message = "Every role carries the permissions boundary."
  }

  assert {
    condition     = length(aws_iam_role_policy.this) == 1 && length(aws_iam_role_policy_attachment.managed) == 0
    error_message = "Inline policy is created only when policy_json is set."
  }
}

run "managed_policy_only" {
  command = apply

  variables {
    policy_json         = null
    managed_policy_arns = ["arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"]
  }

  assert {
    condition     = length(aws_iam_role_policy.this) == 0 && length(aws_iam_role_policy_attachment.managed) == 1
    error_message = "A role with only managed policies has no inline policy."
  }
}
