mock_provider "aws" {}

variables {
  name                     = "shopflow-reaper"
  oidc_provider_arn        = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/shopflow-boundary"
  policy_json              = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  subjects                 = ["repo:winthebest/shopflow:ref:refs/heads/main:job_workflow_ref:winthebest/shopflow/.github/workflows/cloud-reaper.yml@refs/heads/main"]
}

run "exact_subject_for_main_branch_workflow" {
  command = apply

  assert {
    condition = jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Condition == {
      StringEquals = {
        "token.actions.githubusercontent.com:aud" = ["sts.amazonaws.com"]
        "token.actions.githubusercontent.com:sub" = var.subjects
      }
    }
    error_message = "Exact match must check both audience and subject with StringEquals."
  }

  assert {
    condition     = aws_iam_role.this.permissions_boundary == var.permissions_boundary_arn
    error_message = "Every role carries the permissions boundary."
  }
}

run "pull_request_subject_uses_string_like" {
  command = apply

  variables {
    subject_match = "StringLike"
    subjects      = ["repo:winthebest/shopflow:pull_request:job_workflow_ref:winthebest/shopflow/.github/workflows/infra-ci.yml@refs/pull/*/merge"]
  }

  assert {
    condition     = jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] == var.subjects
    error_message = "Pull request refs are matched with StringLike."
  }

  assert {
    condition     = jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == ["sts.amazonaws.com"]
    error_message = "Audience is always checked exactly."
  }
}

run "rejects_subject_without_workflow_pin" {
  command = plan

  variables {
    subjects = ["repo:winthebest/shopflow:*"]
  }

  expect_failures = [var.subjects]
}
