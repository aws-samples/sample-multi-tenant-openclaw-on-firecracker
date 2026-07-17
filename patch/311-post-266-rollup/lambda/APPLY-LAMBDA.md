# Patch 311 — Layer 2: Lambda code fixes (redeploy the function)

These fixes live in the control-plane API Lambda. They cannot be applied by
swapping a host script — the Lambda function code must be updated.

## What this layer fixes

| Fix | File | What it solves | Who needs it |
| --- | ---- | -------------- | ------------ |
| Private-API routing | `deploy/lambda/api/handler.py` | On a private API (a `{proxy+}` integration), `event["resource"]` is always `/{proxy+}`, so the handler's resource-template dispatch matches nothing and every route except `/ping` returns 404. The fix resolves the concrete path against the registered route templates. | **Private-API deployments only** (public API Gateway is unaffected). |
| Rebuild / restart semantics | `deploy/lambda/api/services/tenant_service.py`, `host_service.py` | A rootfs upgrade must go through `rebuild` (drop the overlay + verify adoption), not `restart` (which keeps the old overlay -> half-new/half-old). Adds adoption verification so the version is stamped only after the VM actually boots the new rootfs, and keeps an existing data disk instead of rebuilding it on a template size drift (which would lose data). | Any deployment that upgrades images / rebuilds. |

## How to apply

These are CDK-managed Lambdas (source under `deploy/lambda/api/`). Either method:

### Method A: full `cdk deploy` (recommended — also carries Layer 3)

```bash
# From the repo root, using your deploy method (see the top-level README / setup.sh):
bash setup.sh <region> <profile-or-dash>   # pass "-" as the profile to use the instance role
# or: cdk deploy OpenClawOrchestrator --require-approval never -c region=<region>
```

`cdk deploy` repackages the latest `deploy/lambda/api/` source onto the API Lambda,
applying both fixes at once. This is also how Layer 3 (the IAM grant and the
VPC-endpoint toggle) takes effect — one deploy covers everything.

### Method B: update the API Lambda code only (no full stack deploy)

```bash
# 1. Package the API Lambda source (from the repo root)
cd deploy/lambda/api && zip -r /tmp/api-lambda.zip . && cd -
# 2. Find the API function name (from stack outputs or the console; usually
#    contains "OpenClawOrchestrator" and "ApiFn")
FN=$(aws lambda list-functions --region <region> \
  --query "Functions[?contains(FunctionName,'ApiFn')].FunctionName" --output text)
# 3. Update the code
aws lambda update-function-code --function-name "$FN" \
  --zip-file fileb:///tmp/api-lambda.zip --region <region>
```

> Method B updates code only; it does **not** apply the Layer 3 IAM grant or the
> VPC-endpoint toggle. If the host role does not yet have read access to
> `openclaw-tenant-secrets`, run `../iam/apply-iam.sh` first (see the dependency
> order in APPLY-INSTRUCTIONS).

## Verify

- **Private-API routing:** on a private API, `curl` a non-`/ping` route (e.g.
  `GET /tenants`); it should respond instead of returning 404.
- **Rebuild semantics:** rebuild a tenant (the image-upgrade path) and confirm
  (1) the existing data disk is preserved, and (2) the version number updates only
  after the VM actually boots the new rootfs.
