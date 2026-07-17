# Patch 311 — Layer 3: CDK / permissions / VPC endpoint

This layer is infrastructure — it cannot be applied by swapping files; it needs
`cdk deploy` (or the manual equivalents below). This is where the **dependency and
permission** concerns live.

## What this layer fixes

| Fix | File | What it solves |
| --- | ---- | -------------- |
| Host-role read grant | `deploy/stacks/compute.py` | Grants the host instance role read access (`GetItem`) to `openclaw-tenant-secrets`. This is a **prerequisite of the token fallback** — without it, `launch-vm.sh` gets AccessDenied when it reads the token and the VM never starts. |
| Secrets Manager VPC-endpoint toggle | `deploy/stacks/observability.py`, `config.yml.example` | Adds `logging.aos.create_secretsmanager_vpce` (default `true`). Prevents a private-DNS conflict when the VPC already has a Secrets Manager endpoint, which would otherwise roll back the whole stack. |

## Dependency / permission — read before deploying

### 1. Host-role read grant is a fail-closed prerequisite (handle first)

On the recovery path, `launch-vm.sh` reads the token from `openclaw-tenant-secrets`
and **aborts the launch if the read is denied**. So before replacing host scripts
or rebuilding tenants, the host role must already be able to read that table.

- **CDK-managed deployment:** `cdk deploy` includes the grant.
- **Cannot deploy yet:** run `../iam/apply-iam.sh <host-role> <region> <account>`
  to apply the equivalent inline policy (you can remove it after a later deploy).

Verify on the host (returns `{}`/an item, not AccessDenied):

```bash
aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{"tenant_id":{"S":"__probe__"}}' --region <region>
```

### 2. Secrets Manager VPC-endpoint conflict (check before deploy)

AWS allows only **one interface VPC endpoint per service per VPC with private DNS
enabled**. If your VPC already has a Secrets Manager endpoint (left over from a
prior deploy, or one you created), a plain `cdk deploy` tries to create a second
one with private DNS and **conflicts, rolling back the whole stack**.

Check before deploying:

```bash
aws ec2 describe-vpc-endpoints --region <region> \
  --filters "Name=service-name,Values=com.amazonaws.<region>.secretsmanager" \
            "Name=vpc-id,Values=<your-vpc-id>" \
  --query 'VpcEndpoints[].[VpcEndpointId,PrivateDnsEnabled]' --output text
```

- **Empty** (no existing endpoint) -> keep the default `create_secretsmanager_vpce:
  true`; the stack creates it.
- **An existing endpoint with private DNS** -> choose one:
  - **(a) Reuse it (recommended):** set `logging.aos.create_secretsmanager_vpce:
    false` in `config.yml`. The stack skips its own endpoint and relies on the
    existing one (private DNS resolves the standard
    `secretsmanager.<region>.amazonaws.com` name to it, so the Lambda works
    transparently).
  - **(b) Delete the stale endpoint, then let the stack create it:** only if that
    endpoint is truly unused —
    `aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <id>` — then deploy. Delete
    only the Secrets Manager endpoint in this VPC; never touch endpoints for other
    services (e.g. execute-api).

## How to apply

```bash
# From the repo root (config.yml already set per the VPC-endpoint check above):
bash setup.sh <region> <profile-or-dash>       # pass "-" as the profile to use the instance role
# or: cdk deploy OpenClawOrchestrator --require-approval never -c region=<region>
```

A single deploy covers the IAM grant, the VPC-endpoint toggle, and the Lambda code.

## Verify

- Grant: the get-item probe above no longer returns AccessDenied.
- VPC endpoint: the deploy no longer rolls back on a private-DNS conflict; the
  stack reaches CREATE_COMPLETE.
