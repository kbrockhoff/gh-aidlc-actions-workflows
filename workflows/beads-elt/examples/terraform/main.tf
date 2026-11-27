# Example: IAM role for the beads-elt reusable workflow, assumed through GitHub OIDC,
# with the least-privilege S3 Tables policy from workflows/beads-elt/README.md.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  oidc_host = "token.actions.githubusercontent.com"

  oidc_provider_arn = (var.create_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  )

  table_bucket_arn = (var.create_table_bucket
    ? aws_s3tables_table_bucket.raw[0].arn
    : "arn:${data.aws_partition.current.partition}:s3tables:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:bucket/${var.table_bucket_name}"
  )

  allowed_subjects = [
    for repo in var.github_repositories :
    var.workflow_ref == null ? "repo:${repo}:*" : "repo:${repo}:job_workflow_ref:${var.workflow_ref}"
  ]
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  tags           = var.tags
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1

  url = "https://${local.oidc_host}"
}

resource "aws_s3tables_table_bucket" "raw" {
  count = var.create_table_bucket ? 1 : 0

  name = var.table_bucket_name
  tags = var.tags

  encryption_configuration = var.kms_key_arn == null ? null : {
    sse_algorithm = "aws:kms"
    kms_key_arn   = var.kms_key_arn
  }
}

resource "aws_s3tables_namespace" "raw" {
  count = var.create_namespace ? 1 : 0

  namespace        = var.namespace
  table_bucket_arn = local.table_bucket_arn
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_host}:sub"
      values   = local.allowed_subjects
    }
  }
}

data "aws_iam_policy_document" "s3tables" {
  statement {
    sid       = "ReadCatalogConfig"
    actions   = ["s3tables:GetTableBucket"]
    resources = [local.table_bucket_arn]
  }

  statement {
    sid       = "UseRawNamespace"
    actions   = ["s3tables:GetNamespace", "s3tables:CreateTable"]
    resources = [local.table_bucket_arn]

    condition {
      test     = "StringEquals"
      variable = "s3tables:namespace"
      values   = [var.namespace]
    }
  }

  statement {
    sid = "ReadWriteRawTables"
    actions = [
      "s3tables:GetTableMetadataLocation",
      "s3tables:UpdateTableMetadataLocation",
      "s3tables:GetTableData",
      "s3tables:PutTableData",
    ]
    resources = ["${local.table_bucket_arn}/table/*"]

    condition {
      test     = "StringEquals"
      variable = "s3tables:namespace"
      values   = [var.namespace]
    }
  }

  dynamic "statement" {
    for_each = var.create_namespace ? [] : [1]
    content {
      sid       = "CreateRawNamespace"
      actions   = ["s3tables:CreateNamespace"]
      resources = [local.table_bucket_arn]
    }
  }

  dynamic "statement" {
    for_each = var.kms_key_arn == null ? [] : [var.kms_key_arn]
    content {
      sid       = "UseTableKey"
      actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role" "beads_elt" {
  name                 = var.role_name
  description          = "Assumed by the beads-elt GitHub Actions workflow to load the raw zone"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
  tags                 = var.tags
}

resource "aws_iam_role_policy" "s3tables" {
  name   = "beads-elt-s3tables"
  role   = aws_iam_role.beads_elt.id
  policy = data.aws_iam_policy_document.s3tables.json
}
