# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "api_url" {
  description = "API Gateway invoke URL"
  value       = aws_api_gateway_rest_api.api.execution_arn
}

output "tenants_table" {
  description = "DynamoDB table for tenants"
  value       = aws_dynamodb_table.tenants.name
}

output "hosts_table" {
  description = "DynamoDB table for hosts"
  value       = aws_dynamodb_table.hosts.name
}

output "assets_bucket" {
  description = "S3 bucket for build assets and tenant backups"
  value       = aws_s3_bucket.assets.bucket
}

output "lambda_function_name" {
  description = "Name of the API Lambda function"
  value       = aws_lambda_function.api.function_name
}
