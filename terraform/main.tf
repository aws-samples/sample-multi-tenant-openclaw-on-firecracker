# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Terraform module — parity with the CDK stack in `deploy/stack.py`.
# This is a deliberately minimal blueprint that gets a Terraform user
# to a working orchestrator core; the full CDK stack ships extras
# (CloudFront, Cognito, AgentCore, AMP/AMG) that you can layer on
# from the AWS provider's reference docs.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

# ─────────────────────────────────────────────
# DynamoDB
# ─────────────────────────────────────────────

resource "aws_dynamodb_table" "tenants" {
  name         = "openclaw-tenants"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "hosts" {
  name         = "openclaw-hosts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "instance_id"

  attribute {
    name = "instance_id"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# ─────────────────────────────────────────────
# S3 assets bucket
# ─────────────────────────────────────────────

resource "aws_s3_bucket" "assets" {
  bucket = "openclaw-assets-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    id     = "backup-expiration"
    status = "Enabled"
    filter {
      prefix = "${var.backup_prefix}/"
    }
    expiration {
      days = var.backup_retention_days
    }
  }
}

# ─────────────────────────────────────────────
# IAM role for Lambda
# ─────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "openclaw-api-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_inline" {
  name = "openclaw-api-inline"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:DeleteItem", "dynamodb:Scan", "dynamodb:Query",
        ]
        Resource = [
          aws_dynamodb_table.tenants.arn,
          aws_dynamodb_table.hosts.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.assets.arn,
          "${aws_s3_bucket.assets.arn}/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:SendCommand", "ssm:GetCommandInvocation",
          "ec2:DescribeInstances", "ec2:TerminateInstances",
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:SetDesiredCapacity",
          "autoscaling:CompleteLifecycleAction",
          "autoscaling:TerminateInstanceInAutoScalingGroup",
        ]
        Resource = "*"
      },
    ]
  })
}

# ─────────────────────────────────────────────
# Lambda
# ─────────────────────────────────────────────

resource "aws_lambda_function" "api" {
  function_name    = "openclaw-api"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 120
  memory_size      = 256
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)

  environment {
    variables = {
      TENANTS_TABLE  = aws_dynamodb_table.tenants.name
      HOSTS_TABLE    = aws_dynamodb_table.hosts.name
      ASSETS_BUCKET  = aws_s3_bucket.assets.bucket
      ROOTFS_PREFIX  = var.rootfs_prefix
      BACKUP_PREFIX  = var.backup_prefix
      ASG_NAME       = "openclaw-hosts-asg"
    }
  }
}

# ─────────────────────────────────────────────
# API Gateway (REST, HTTP API also acceptable)
# ─────────────────────────────────────────────

resource "aws_api_gateway_rest_api" "api" {
  name = "openclaw-orchestrator"
}

resource "aws_api_gateway_resource" "tenants" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "tenants"
}

resource "aws_api_gateway_method" "tenants_get" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.tenants.id
  http_method   = "GET"
  authorization = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "tenants_get" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.tenants.id
  http_method             = aws_api_gateway_method.tenants_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGW"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}
