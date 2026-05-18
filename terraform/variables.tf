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

variable "backup_retention_days" {
  description = "Days to keep tenant backup archives"
  type        = number
  default     = 7
}
