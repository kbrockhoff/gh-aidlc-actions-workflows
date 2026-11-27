output "aws_role_arn" {
  description = "Pass as the aws-role-arn input of the beads-elt workflow"
  value       = aws_iam_role.beads_elt.arn
}

output "table_bucket_arn" {
  description = "Pass as the table-bucket-arn input of the beads-elt workflow"
  value       = local.table_bucket_arn
}

output "aws_region" {
  description = "Pass as the aws-region input of the beads-elt workflow"
  value       = data.aws_region.current.region
}

output "namespace" {
  description = "Pass as the namespace input of the beads-elt workflow"
  value       = var.namespace
}
