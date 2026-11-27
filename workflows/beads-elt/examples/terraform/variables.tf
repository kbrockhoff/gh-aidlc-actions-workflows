variable "github_repositories" {
  description = <<-EOT
    Repositories allowed to assume the role, written as they appear in the OIDC sub claim:
    owner/name for legacy subjects, or owner@OWNER_ID/name@REPO_ID for repositories that use
    immutable subjects (the default for repositories created after 2026-07-15). Get the value
    with: gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
    and drop the leading "repo:".
  EOT
  type        = list(string)

  validation {
    condition     = length(var.github_repositories) > 0 && alltrue([for r in var.github_repositories : can(regex("^[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", r))])
    error_message = "Give at least one repository, each as owner/name or owner@OWNER_ID/name@REPO_ID."
  }
}

variable "workflow_ref" {
  description = <<-EOT
    Optional job_workflow_ref pattern, for example
    "kbrockhoff/gh-aidlc-actions-workflows/.github/workflows/beads-elt.yml@*". When set, the
    trust policy expects each repository's OIDC sub to be customized to include repo and
    job_workflow_ref (see the beads-elt README).
  EOT
  type        = string
  default     = null
}

variable "role_name" {
  description = "Name of the IAM role the workflow assumes"
  type        = string
  default     = "beads-elt"
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false if the account already has one."
  type        = bool
  default     = true
}

variable "table_bucket_name" {
  description = "Name of the S3 Tables table bucket for the raw zone"
  type        = string
  default     = "aidlc-raw"
}

variable "create_table_bucket" {
  description = "Create the table bucket. Set false to use an existing bucket with table_bucket_name."
  type        = bool
  default     = true
}

variable "namespace" {
  description = "Raw-zone namespace the workflow writes to"
  type        = string
  default     = "raw_beads"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9_]{0,253}[a-z0-9])?$", var.namespace))
    error_message = "Use lowercase letters, digits and underscores, starting and ending with a letter or digit."
  }
}

variable "create_namespace" {
  description = <<-EOT
    Create the namespace here. When false, the role gets s3tables:CreateNamespace on the
    bucket so the workflow can create it, which can't be limited to one namespace.
  EOT
  type        = bool
  default     = true
}

variable "kms_key_arn" {
  description = <<-EOT
    Optional customer managed KMS key for SSE-KMS. Grants the role kms:GenerateDataKey and
    kms:Decrypt, and encrypts a newly created bucket with it. The key policy must also allow
    maintenance.s3tables.amazonaws.com to use the key.
  EOT
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags for the IAM role, OIDC provider and table bucket"
  type        = map(string)
  default     = {}
}
