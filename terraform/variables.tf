# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

variable "region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-northeast-1"
}

variable "lambda_zip_path" {
  description = "Path to a zipped deploy/lambda/api directory"
  type        = string
  default     = "../deploy/lambda/api.zip"
}

variable "rootfs_prefix" {
  description = "S3 key prefix for VM rootfs assets"
  type        = string
  default     = "deployment/rootfs"
}

variable "backup_prefix" {
  description = "S3 key prefix for tenant backups"
  type        = string
  default     = "backups"
}

variable "cpu_overcommit_ratio" {
  description = "Allocatable vCPU multiplier per host (parity with config.yml host.cpu_overcommit_ratio; keep ≤ 4.0)"
  type        = number
  default     = 2.0
}

variable "mem_overcommit_ratio" {
  description = "Allocatable memory multiplier per host (>1.0 requires balloon; keep ≤ 4.0)"
  type        = number
  default     = 1.0
}

variable "max_vms_per_host" {
  description = "Absolute per-host microVM ceiling (0 = ratio-only, no cap) — issue #77"
  type        = number
  default     = 0
}

variable "host_pick_top_k" {
  description = "How many of the least-loaded fitting hosts a create may pick between. 1 restores the old deterministic pick (and its thundering herd on one host's conditional write)."
  type        = number
  default     = 8
}

variable "host_reserve_attempts" {
  description = "Slot-reservation attempts (each on a different host) before queueing the tenant as pending and scaling out."
  type        = number
  default     = 3
}

variable "backup_retention_days" {
  description = "Days to keep tenant backup archives"
  type        = number
  default     = 7
}

# ─────────────────────────────────────────────
# T3-6: API Lambda env parity toggles (were CDK-only)
# ─────────────────────────────────────────────

variable "audit_ttl_days" {
  description = "Audit-log row retention in days (DDB TTL). Parity with config.yml audit.ttl_days."
  type        = number
  default     = 90
}

variable "rbac_enabled" {
  description = "Enable role-based access control (viewer/operator/admin). Requires console auth + Cognito (CDK-only) to be meaningful."
  type        = bool
  default     = false
}

variable "console_auth_enabled" {
  description = "Enable Cognito console login. Note: Cognito resources themselves are CDK-only; set the COGNITO_* env via api_env_overrides on the TF path."
  type        = bool
  default     = false
}

variable "default_no_jwt_role" {
  description = "Role assumed for requests without a JWT when RBAC is on."
  type        = string
  default     = "viewer"
}

variable "vm_data_disk_mb" {
  description = "Per-tenant data disk size in MB. Parity with config.yml (handler fallback is only 2048)."
  type        = number
  default     = 8192
}

variable "api_env_overrides" {
  description = "Extra/override env vars for the api Lambda (e.g. COGNITO_USER_POOL_ID on the TF path). Merged last, wins over all defaults."
  type        = map(string)
  default     = {}
}
