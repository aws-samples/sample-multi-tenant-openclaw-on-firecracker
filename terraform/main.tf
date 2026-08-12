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
# API Lambda static env (T3-6)
# ─────────────────────────────────────────────
# The config-derived env vars the CDK stack sets but the TF path was missing.
# Values mirror config.yml.example defaults; toggles that operators commonly
# flip are exposed as variables. Anything here can be overridden per-deploy via
# var.api_env_overrides without editing this block. A pytest drift-guard asserts
# every os.environ name the handler reads is either here, resource-derived in
# the Lambda block, or explicitly marked CDK-only in README.
locals {
  api_env_static = {
    # Audit log (#71) — retention + the table is wired in the Lambda block.
    AUDIT_TTL_DAYS = tostring(var.audit_ttl_days)
    # RBAC / console auth (#14) — off by default, same as config.yml.example.
    RBAC_ENABLED         = tostring(var.rbac_enabled)
    CONSOLE_AUTH_ENABLED = tostring(var.console_auth_enabled)
    DEFAULT_NO_JWT_ROLE  = var.default_no_jwt_role
    # VM shape / networking defaults — handler fallback VM_DATA_DISK_MB=2048
    # differs from config.yml.example 8192, so TF tenants silently got 2 GB
    # data disks. Pin the config value explicitly.
    VM_DATA_DISK_MB    = tostring(var.vm_data_disk_mb)
    VM_DEFAULT_VCPU    = "2"
    VM_DEFAULT_MEM     = "4096"
    VM_PORT_BASE       = "18789"
    VM_SUBNET_PREFIX   = "172.16"
    HOST_RESERVED_VCPU = "1"
    HOST_RESERVED_MEM  = "2048"
    # T3-1: tenant routing model. per-tenant (default) = one ALB listener rule
    # per tenant, capped by the ALB rules-per-load-balancer QUOTA (default 100 —
    # not the 1-499 priority window the code allocates from). host-tg = one
    # shared TG + /vm/* catch-all + nginx peer-map, so tenants cost no ALB
    # resource and the ceiling becomes hosts x VMs-per-host.
    # Keep per-tenant on the TF path unless the host-tg data plane (shared TG +
    # :8081 peer server) is also provisioned here — it is CDK-only today.
    # GET /admin/routing/status reports the live headroom either way.
    ROUTING_MODE = "per-tenant"
    # Feature flags — default off, parity with config.yml.example.
    QUOTAS_ENABLED          = "false"
    QUOTAS_MAX_VCPU         = "0"
    QUOTAS_MAX_MEM_MB       = "0"
    QUOTAS_MAX_DATA_DISK_MB = "0"
    MULTI_AZ_ENABLED        = "false"
    MULTI_AZ_COUNT          = "1"
    WAF_ENABLED             = "false"
    BALLOON_ENABLED         = "false"
    BALLOON_MIGRATE_MODE    = "cold"
  }
}

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

# T3-6: parity with the CDK stack. Without these tables the api handler's
# fallback kicks in — GROUPS_TABLE unset → groups_table=None → every /groups
# endpoint silently no-ops; AUDIT_TABLE unset → audit_table=None → /audit-log
# returns nothing and zero audit rows are ever written. Both are feature-
# disabling-by-omission bugs on the TF deploy path.
resource "aws_dynamodb_table" "groups" {
  name         = "openclaw-groups"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "name"

  attribute {
    name = "name"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "audit" {
  # Single-partition by design (pk="audit"), ts as range key for time-range
  # queries; DDB TTL on expires_ttl auto-prunes per AUDIT_TTL_DAYS.
  name         = "openclaw-audit-log-tf"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "ts"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "S"
  }

  ttl {
    attribute_name = "expires_ttl"
    enabled        = true
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
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
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
          aws_dynamodb_table.groups.arn,
          aws_dynamodb_table.audit.arn,
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
    # T3-6: env parity with the CDK stack. `local.api_env_static` carries the
    # config-derived vars that were missing on the TF path (feature-gating +
    # VM defaults); resource-derived names (table/bucket/ASG) are merged over
    # it; `var.api_env_overrides` lets operators tune without editing this file.
    # Resource-derived CDK-only vars (COGNITO_*, ALB_LISTENER_ARN, AGENTCORE_*,
    # AMP/GRAFANA) are intentionally absent — see terraform/README.md.
    variables = merge(local.api_env_static, {
      TENANTS_TABLE = aws_dynamodb_table.tenants.name
      HOSTS_TABLE   = aws_dynamodb_table.hosts.name
      GROUPS_TABLE  = aws_dynamodb_table.groups.name
      AUDIT_TABLE   = aws_dynamodb_table.audit.name
      ASSETS_BUCKET = aws_s3_bucket.assets.bucket
      ROOTFS_PREFIX = var.rootfs_prefix
      BACKUP_PREFIX = var.backup_prefix
      ASG_NAME      = "openclaw-hosts-asg"
      # Issue #77 — scheduler parity with the CDK stack. Without these the
      # handler defaults kick in (1.0 / 1.0 / no cap), which differs from
      # config.yml.example and silently changes placement behavior.
      CPU_OVERCOMMIT_RATIO = tostring(var.cpu_overcommit_ratio)
      MEM_OVERCOMMIT_RATIO = tostring(var.mem_overcommit_ratio)
      MAX_VMS_PER_HOST     = tostring(var.max_vms_per_host)
      # Placement anti-herding. A deterministic least-loaded pick made every
      # concurrent create collide on one host's conditional write, and a lost
      # race was then treated as "fleet full" → a bare-metal scale-out. Left at
      # the handler defaults these would still work, but the TF and CDK paths
      # must agree on placement behaviour or the two deployments diverge.
      HOST_PICK_TOP_K       = tostring(var.host_pick_top_k)
      HOST_RESERVE_ATTEMPTS = tostring(var.host_reserve_attempts)
      # T3-1: per-tenant listener-rule priority window. The floor keeps low
      # priorities reserved for static rules (a tenant holding priority 1 fails
      # a later deploy with PriorityInUseException). The ceiling is NOT the real
      # cap — ALB_RULES_QUOTA is, and it is what /admin/routing/status reports
      # as remaining headroom. Raise ALB_RULES_QUOTA only after AWS grants the
      # increase, or the endpoint will over-report capacity.
      PER_TENANT_PRIORITY_MIN = tostring(var.per_tenant_priority_min)
      PER_TENANT_PRIORITY_MAX = tostring(var.per_tenant_priority_max)
      ALB_RULES_QUOTA         = tostring(var.alb_rules_quota)
    }, var.api_env_overrides)
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
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = aws_api_gateway_resource.tenants.id
  http_method      = "GET"
  authorization    = "NONE"
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
