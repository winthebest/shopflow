# Roles the session cluster uses. They are free and live here so the reaper never needs IAM
# permissions: tearing a session down only deletes compute.

resource "aws_iam_role" "eks_cluster" {
  name                 = local.roles.cluster
  permissions_boundary = aws_iam_policy.boundary.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role" "eks_node" {
  name                 = local.roles.node
  permissions_boundary = aws_iam_policy.boundary.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly",
    # vpc-cni (aws-node) runs on the host network and uses the node role; IMDS hop limit 1 still
    # keeps ordinary pods away from these credentials.
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
  ])
  role       = aws_iam_role.eks_node.name
  policy_arn = each.value
}

# ---------------------------------------------------------------------------------------------
# Pod Identity roles, one per service account in contract.pod_identities, each scoped to its
# data prefix. The namespace/service account table is the contract with the Helm charts.

locals {
  pg_backup_objects = "${local.arn.data_bucket}/${local.prefixes.pg_backup}*"
  glue_resources = concat(
    ["${local.arn.glue}:catalog"],
    flatten([for db in local.contract.glue_databases : ["${local.arn.glue}:database/${db}", "${local.arn.glue}:table/${db}/*"]]),
  )

  # ListBucket is limited to the role's prefix when a prefix is sent; HeadBucket (no prefix) stays allowed.
  prefix_statements = { for name, prefix in local.prefixes : name => [
    {
      Sid       = "List${replace(title(replace(name, "_", " ")), " ", "")}Prefix"
      Effect    = "Allow"
      Action    = "s3:ListBucket"
      Resource  = local.arn.data_bucket
      Condition = { StringLikeIfExists = { "s3:prefix" = ["${prefix}*"] } }
    },
    {
      Sid      = "ReadWrite${replace(title(replace(name, "_", " ")), " ", "")}Objects"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      Resource = "${local.arn.data_bucket}/${prefix}*"
    },
  ] }

  bucket_location = {
    Sid      = "BucketLocation"
    Effect   = "Allow"
    Action   = "s3:GetBucketLocation"
    Resource = local.arn.data_bucket
  }

  glue_iceberg = {
    Sid      = "GlueIcebergTables"
    Effect   = "Allow"
    Action   = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:CreateTable", "glue:UpdateTable", "glue:DeleteTable"]
    Resource = local.glue_resources
  }

  pod_policies = {
    "ebs-csi" = {
      json    = null
      managed = ["arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"]
    }
    "aws-lb-controller" = {
      # Upstream policy of the controller version deployed in deploy/platform/aws-lb-controller.
      json    = jsonencode(jsondecode(file("${path.module}/policies/aws-lb-controller-v3.5.0.json")))
      managed = []
    }
    "cnpg" = {
      json    = jsonencode({ Version = "2012-10-17", Statement = concat(local.prefix_statements.pg_backup, [local.bucket_location]) })
      managed = []
    }
    "kafka-connect" = {
      json    = jsonencode({ Version = "2012-10-17", Statement = concat(local.prefix_statements.iceberg, [local.bucket_location, local.glue_iceberg]) })
      managed = []
    }
    "trino" = {
      json = jsonencode({
        Version = "2012-10-17"
        Statement = concat(local.prefix_statements.iceberg, [local.bucket_location, local.glue_iceberg, {
          Sid      = "NeverReadDatabaseBackups"
          Effect   = "Deny"
          Action   = "s3:*"
          Resource = local.pg_backup_objects
        }])
      })
      managed = []
    }
    "flink" = {
      json = jsonencode({
        Version = "2012-10-17"
        Statement = concat(
          local.prefix_statements.flink_checkpoints,
          [local.bucket_location],
          [for s in concat(local.prefix_statements.iceberg, [local.glue_iceberg]) : s if var.flink_writes_iceberg],
        )
      })
      managed = []
    }
    "external-secrets" = {
      json = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          Sid      = "AssumeNamespaceRoles"
          Effect   = "Allow"
          Action   = ["sts:AssumeRole", "sts:TagSession"]
          Resource = "${local.arn.role}/${local.roles.eso_prefix}*"
        }]
      })
      managed = []
    }
  }
}

module "pod_role" {
  source   = "../modules/pod-identity-role"
  for_each = local.contract.pod_identities

  name                     = local.pod_role_names[each.key]
  namespace                = each.value.namespace
  service_account          = each.value.service_account
  cluster_arn              = local.arn.cluster
  account_id               = local.account_id
  permissions_boundary_arn = aws_iam_policy.boundary.arn
  policy_json              = local.pod_policies[each.key].json
  managed_policy_arns      = local.pod_policies[each.key].managed
}

# ---------------------------------------------------------------------------------------------
# External Secrets: one SecretStore per namespace, each assuming its own role that can read only
# /shopflow/aws/<namespace>/*. The ESO controller (Pod Identity) can do nothing but assume them.

resource "aws_iam_role" "eso_namespace" {
  for_each             = toset(local.contract.eso_namespaces)
  name                 = "${local.roles.eso_prefix}${each.value}"
  permissions_boundary = aws_iam_policy.boundary.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = module.pod_role["external-secrets"].role_arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy" "eso_namespace" {
  for_each = toset(local.contract.eso_namespaces)
  name     = "read-${each.key}-parameters"
  role     = aws_iam_role.eso_namespace[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadNamespaceParameters"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        Resource = ["${local.arn.parameter}${local.contract.ssm.prefix}/${each.key}", "${local.arn.parameter}${local.contract.ssm.prefix}/${each.key}/*"]
      },
      {
        Sid       = "DecryptWithSsmKey"
        Effect    = "Allow"
        Action    = "kms:Decrypt"
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "ssm.${local.region}.amazonaws.com" } }
      },
    ]
  })
}

# Contract checks that would otherwise fail late or silently.
resource "terraform_data" "contract_checks" {
  lifecycle {
    precondition {
      condition     = alltrue([for key in keys(local.contract.pod_identities) : contains(keys(local.pod_policies), key)])
      error_message = "Every entry of cloud-contract.json pod_identities needs a policy in local.pod_policies."
    }
    precondition {
      condition     = !contains(local.contract.eso_namespaces, "control")
      error_message = "\"control\" is reserved for /shopflow/aws/control/* (lease, backup pointer); it cannot be an ESO namespace."
    }
  }
}
