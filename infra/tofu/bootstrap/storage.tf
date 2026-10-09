# Buckets and catalog that outlive every cluster: OpenTofu state, and the lakehouse/backup data.
# Encryption uses SSE-S3: a customer KMS key costs $1/month and adds nothing for this threat model
# (single-owner account, Block Public Access, TLS-only bucket policies).

locals {
  buckets = {
    state = local.state_bucket
    data  = local.data_bucket
  }

  # Noncurrent versions expire after 7 days on these prefixes only. pg-backup/ has no lifecycle
  # rule: CloudNativePG's retentionPolicy owns backup expiry, and a second policy could delete a
  # base backup that the recovery chain still points at.
  noncurrent_expiry_prefixes = {
    iceberg           = local.prefixes.iceberg
    flink-checkpoints = local.prefixes.flink_checkpoints
    evidence          = local.prefixes.evidence
  }
}

#trivy:ignore:AWS-0089 Access logging would need a third bucket; CloudTrail data events are the upgrade path if needed.
resource "aws_s3_bucket" "this" {
  for_each = local.buckets
  bucket   = each.value

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.this[each.key].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = local.buckets
  bucket                  = aws_s3_bucket.this[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.this[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

#trivy:ignore:AWS-0132 SSE-S3 instead of a customer KMS key (cost); see the comment at the top of this file.
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.this[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.this[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.this[each.key].arn, "${aws_s3_bucket.this[each.key].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.this]
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.this["data"].id

  dynamic "rule" {
    for_each = local.noncurrent_expiry_prefixes
    content {
      id     = "expire-noncurrent-${rule.key}"
      status = "Enabled"

      filter {
        prefix = rule.value
      }

      noncurrent_version_expiration {
        noncurrent_days = 7
      }

      abort_incomplete_multipart_upload {
        days_after_initiation = 7
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

# Trino maps each schema of the `lake` catalog to a Glue database, so the schemas are created here
# (once) instead of by dbt or Trino at runtime.
resource "aws_glue_catalog_database" "lake" {
  for_each     = toset(local.contract.glue_databases)
  name         = each.value
  description  = "Iceberg schema ${each.value} of the Trino catalog lake."
  location_uri = "s3://${local.data_bucket}/${local.prefixes.iceberg}${each.value}"
}

# Created here so control plane logs outlive each session cluster (EKS writes into an existing group).
#trivy:ignore:AWS-0017 Default CloudWatch encryption; a customer key costs $1/month.
resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${local.cluster}/cluster"
  retention_in_days = var.eks_log_retention_days
}
