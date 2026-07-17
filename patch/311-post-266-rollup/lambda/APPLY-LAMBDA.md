# Patch 311 — Layer 2: Lambda code fixes (redeploy the function)

These fixes live in the control-plane API Lambda. They cannot be applied by
swapping a host script — the Lambda function code must be updated.

## What this layer fixes

| Fix                         | File                                                              | What it solves                                                                                                                                                                                                                                                                                                                                                      | Who needs it                                                         |
| --------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Private-API routing         | `deploy/lambda/api/handler.py`                                    | On a private API (a `{proxy+}` integration), `event["resource"]` is always `/{proxy+}`, so the handler's resource-template dispatch matches nothing and every route except `/ping` returns 404. The fix resolves the concrete path against the registered route templates.                                                                                          | **Private-API deployments only** (public API Gateway is unaffected). |
| Rebuild / restart semantics | `deploy/lambda/api/services/tenant_service.py`, `host_service.py` | A rootfs upgrade must go through `rebuild` (drop the overlay + verify adoption), not `restart` (which keeps the old overlay -> half-new/half-old). Adds adoption verification so the version is stamped only after the VM actually boots the new rootfs, and keeps an existing data disk instead of rebuilding it on a template size drift (which would lose data). | Any deployment that upgrades images / rebuilds.                      |

## How to apply — `update-function-code` only (NO cdk deploy)

The source lives under `deploy/lambda/api/`. **Do NOT `cdk deploy` / run `setup.sh`** — this
deployment was manually modified after its original deploy, and a stack deploy would
overwrite those changes. Update just the function code:

Permissions: `lambda:ListFunctions`, `lambda:UpdateFunctionCode`.

```bash
# 1. Package the API Lambda source (from the repo root of a checkout at patch_sha)
cd deploy/lambda/api && zip -r /tmp/api-lambda.zip . && cd -
# 2. Find the API function name (stack outputs or console; contains "ApiFn")
FN=$(aws lambda list-functions --region <region> \
  --query "Functions[?contains(FunctionName,'ApiFn')].FunctionName" --output text)
# confirm FN resolved to exactly ONE function name before proceeding
echo "$FN"
# 3. Update the code
aws lambda update-function-code --function-name "$FN" \
  --zip-file fileb:///tmp/api-lambda.zip --region <region>
```

> This updates code only; it does **not** apply the IAM grant or the VPCE. Those are
> separate layers — do `iam/apply-iam.sh` first (fail-closed prerequisite) and handle the
> VPCE via `network/APPLY-NETWORK.md`. Rollback: re-run `update-function-code` with a zip
> built from the previous `base_sha` checkout, or `aws lambda update-function-code` to the
> prior `$LATEST` published version if you version the function.

## Verify

- **Private-API routing:** on a private API, `curl` a non-`/ping` route (e.g.
  `GET /tenants`); it should respond instead of returning 404.
- **Rebuild semantics:** rebuild a tenant (the image-upgrade path) and confirm
  (1) the existing data disk is preserved, and (2) the version number updates only
  after the VM actually boots the new rootfs.
