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

## Variables

| Name | Default | Purpose |
|---|---|---|
| `region` | `ap-northeast-1` | AWS region |
| `lambda_zip_path` | `../deploy/lambda/api.zip` | Where Terraform finds the Lambda package |
| `rootfs_prefix` | `deployment/rootfs` | S3 prefix for VM rootfs assets |
| `backup_prefix` | `backups` | S3 prefix for tenant backups |
| `backup_retention_days` | `7` | S3 lifecycle expiration window |

## Outputs

`api_url`, `tenants_table`, `hosts_table`, `assets_bucket`, `lambda_function_name`.

## Why two deployment paths?

- **CDK** (canonical) — full feature set, tracks main rapidly.
- **Terraform** (this module) — for sites that mandate Terraform, with the trade-off that some advanced features lag.

If you find yourself adding significant resources here that the CDK stack already has, please cross-reference `deploy/stack.py` to keep semantics aligned.
