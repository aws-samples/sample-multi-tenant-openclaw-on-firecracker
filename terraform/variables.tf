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

variable "backup_retention_days" {
  description = "Days to keep tenant backup archives"
  type        = number
  default     = 7
}
