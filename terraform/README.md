# OpenClaw Terraform Module (issue #18)

This module provides a Terraform deployment path **at parity with the CDK stack** in `deploy/stack.py`. It's intended as a starting point for shops standardized on Terraform; the CDK stack remains the canonical, fully-featured deployment.

## Quick start

```bash
cd terraform/

# Package the Lambda code so Terraform can upload it.
( cd ../deploy/lambda/api && zip -qr ../api.zip . )

terraform init
terraform plan -var "region=ap-northeast-1"
terraform apply -var "region=ap-northeast-1"
```

## What this module covers

The minimum viable orchestrator core:

| Resource | Terraform | CDK equivalent |
|---|---|---|
| Tenants table | `aws_dynamodb_table.tenants` | `dynamodb.Table` "Tenants" |
| Hosts table | `aws_dynamodb_table.hosts` | `dynamodb.Table` "Hosts" |
| Groups table | `aws_dynamodb_table.groups` | `dynamodb.Table` "Groups" |
| Audit-log table | `aws_dynamodb_table.audit` (TTL on `expires_ttl`) | `dynamodb.Table` "Audit" |
| Assets bucket | `aws_s3_bucket.assets` + lifecycle | `s3.Bucket` + `cr.AwsCustomResource` |
| API Lambda role | `aws_iam_role.lambda_exec` + inline policy | `_lambda.Function` execution role |
| API Lambda | `aws_lambda_function.api` | `_lambda.Function` "ApiHandler" |
| API Gateway | `aws_api_gateway_rest_api` + method/integration | `apigw.RestApi` |

## What's intentionally NOT here

The CDK stack does much more. This module **does not** include:

- VPC + ASG + EC2 launch template (the host fleet)
- ALB + listener rules for tenant dashboards
- CloudFront distribution + custom domain
- Cognito User Pool + groups (RBAC, issue #14)
- Amazon Managed Prometheus + Grafana (issue #4)
- AgentCore wiring
- WAF rules

Add them progressively — the AWS provider has 1:1 parity for every CDK construct used. The CDK stack is the reference; cross-check `deploy/stack.py` for the exact semantics you need.

## Env-var parity (T3-6)

Earlier this module set only 9 of the ~42 env vars the api Lambda receives from CDK, so several features **silently no-op'd** on the TF path (the handler falls back to feature-off defaults, without error):

- `GROUPS_TABLE` unset → `/groups` endpoints do nothing;
- `AUDIT_TABLE` unset → `/audit-log` empty, no audit rows written;
- `RBAC_ENABLED` / `CONSOLE_AUTH_ENABLED` unset → RBAC silently off;
- `VM_DATA_DISK_MB` unset → tenants got 2 GB disks (handler default) instead of the configured 8 GB.

Fixed: the groups + audit tables now exist here, and `local.api_env_static` supplies the config-derived vars (merged with resource-derived table/bucket names and `var.api_env_overrides`). A pytest drift-guard (`tests/test_terraform.py`) fails CI if the handler starts reading an env var the TF path doesn't provide.

**Intentionally CDK-only** (resource-derived, no TF equivalent in this minimal module — set them via `api_env_overrides` if you add those resources yourself): `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `ALB_LISTENER_ARN`, `VPC_ID`, `AGENTCORE_*`, `AMP_REMOTE_WRITE_URL`, `GRAFANA_WORKSPACE_URL`, `HEALTH_CHECK_FUNCTION`.

> Note: `local.api_env_static` mirrors `config.yml.example` defaults, not your `config.yml`. If you tuned `config.yml`, pass the same values via `-var 'api_env_overrides={...}'`.

## Variables

| Name | Default | Purpose |
|---|---|---|
| `region` | `ap-northeast-1` | AWS region |
| `lambda_zip_path` | `../deploy/lambda/api.zip` | Where Terraform finds the Lambda package |
| `rootfs_prefix` | `deployment/rootfs` | S3 prefix for VM rootfs assets |
| `backup_prefix` | `backups` | S3 prefix for tenant backups |
| `backup_retention_days` | `7` | S3 lifecycle expiration window |
| `cpu_overcommit_ratio` | `2.0` | Allocatable vCPU multiplier (#77 scheduler parity) |
| `mem_overcommit_ratio` | `1.0` | Allocatable memory multiplier (>1.0 needs balloon) |
| `max_vms_per_host` | `0` | Absolute per-host microVM ceiling (0 = ratio-only) |
| `audit_ttl_days` | `90` | Audit-log row retention (DDB TTL) |
| `rbac_enabled` | `false` | Role-based access control toggle |
| `console_auth_enabled` | `false` | Cognito console login toggle |
| `default_no_jwt_role` | `viewer` | Role for requests without a JWT when RBAC on |
| `vm_data_disk_mb` | `8192` | Per-tenant data disk MB (handler fallback is only 2048) |
| `api_env_overrides` | `{}` | Extra/override api Lambda env vars (e.g. `COGNITO_*`) |

## Outputs

`api_url`, `tenants_table`, `hosts_table`, `assets_bucket`, `lambda_function_name`.

## Why two deployment paths?

- **CDK** (canonical) — full feature set, tracks main rapidly.
- **Terraform** (this module) — for sites that mandate Terraform, with the trade-off that some advanced features lag.

If you find yourself adding significant resources here that the CDK stack already has, please cross-reference `deploy/stack.py` to keep semantics aligned.
