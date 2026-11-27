terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.21.0 added tags to aws_s3tables_table_bucket
      version = ">= 6.21, < 7.0"
    }
  }
}
