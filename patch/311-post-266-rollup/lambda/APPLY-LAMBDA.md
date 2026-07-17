# Patch 311 — Layer 2: Lambda code fixes (redeploy the function)

These fixes live in the control-plane API Lambda. They cannot be applied by
swapping a host script — the Lambda function code must be updated.

## What this layer fixes

| Fix                                   | File                                                              | What it solves                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Who needs it                                                         |
| ------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Private-API routing                   | `deploy/lambda/api/handler.py`                                    | On a private API (a `{proxy+}` integration), `event["resource"]` is always `/{proxy+}`, so the handler's resource-template dispatch matches nothing and every route except `/ping` returns 404. The fix resolves the concrete path against the registered route templates.                                                                                                                                                                                                                                                | **Private-API deployments only** (public API Gateway is unaffected). |
| Rebuild / restart semantics           | `deploy/lambda/api/services/tenant_service.py`, `host_service.py` | A rootfs upgrade must go through `rebuild` (drop the overlay + verify adoption), not `restart` (which keeps the old overlay -> half-new/half-old). Adds adoption verification so the version is stamped only after the VM actually boots the new rootfs, and keeps an existing data disk instead of rebuilding it on a template size drift (which would lose data).                                                                                                                                                       | Any deployment that upgrades images / rebuilds.                      |
| Paired-device persistence (#312/#314) | `deploy/lambda/api/services/tenant_service.py`                    | On VM recovery / image-update relaunch, the host re-injects `paired.json` from `device_paired_b64`. `create_tenant` now persists that value to the tenants table (no TTL) via `persist_device_paired_b64`, with one retry and fail-loud logging, so the re-injection has a long-term source. The paired blob is public (deviceId + publicKey + roles + scopes, no private key); the gateway token is NOT stored this way (it is a short-lived secret). Pairs with the launch-vm.sh re-injection in Layer `host-scripts/`. | Any deployment where VMs recover / get their image updated.          |

## How to apply — `update-function-code` only (NO cdk deploy)

The full Lambda source is **shipped in this patch** under `lambda/api/` (36 files, byte-for-byte
the repo's `deploy/lambda/api/` at `patch_sha` — you do not need a repo checkout). **Do NOT
`cdk deploy` / run `setup.sh`** — this deployment was manually modified after its original
deploy, and a stack deploy would overwrite those changes. Update just the function code — but
three things make a naive `zip -r . && update-function-code` wrong here, so follow the steps exactly:

1. **Dependencies must be bundled.** The handler hard-imports `aws_lambda_powertools` (and
   uses `PyJWT` + `cryptography` for Cognito RS256), so a source-only zip fails at cold start
   with `Unable to import module`. `deploy/lambda/api/requirements.txt` lists them; they must
   be `pip install`-ed for the Lambda runtime's platform (ARM64 / manylinux2014_aarch64).
2. **This same code is deployed as TWO functions.** `openclaw-api` (the control-plane API)
   AND `openclaw-lifecycle-consumer` (the SQS worker that actually runs `create_tenant` when
   the lifecycle queue is enabled). #312/#314 live on the create path, so **both** must be
   updated or the consumer path keeps running old code.
3. **API Gateway invokes the `live` alias, not `$LATEST`.** `update-function-code` only moves
   `$LATEST`; you must `publish-version` then `update-alias --name live` for it to take effect.
   (The consumer's SQS event source triggers `$LATEST` directly — no alias step needed there.)

Permissions: `lambda:GetFunction`, `lambda:UpdateFunctionCode`, `lambda:PublishVersion`,
`lambda:UpdateAlias`, `lambda:GetAlias`. Docker (or a local ARM64 build env) is needed to bundle the deps.

```bash
# 0. RECORD THE ROLLBACK POINT FIRST — capture what `live` points at NOW, before touching anything.
#    Save these two values; Step "Rollback" below flips straight back to them.
ROLLBACK_VER=$(aws lambda get-alias --function-name openclaw-api --name live \
  --region <region> --query FunctionVersion --output text)
echo "ROLLBACK: openclaw-api live currently -> version $ROLLBACK_VER  (save this)"
# consumer runs on $LATEST; record its current code sha to detect/confirm the change:
aws lambda get-function --function-name openclaw-lifecycle-consumer --region <region> \
  --query 'Configuration.CodeSha256' --output text 2>/dev/null | tee /tmp/consumer-codesha-before.txt

# 1. Build the deployment package WITH dependencies (ARM64 wheels). The FULL Lambda source is
#    shipped in THIS patch under `lambda/api/` — no repo checkout needed, this patch is
#    self-contained. Mirror the CDK bundling (install for the Lambda runtime, not the host).
cd "$(dirname "$0")/api" 2>/dev/null || cd patch/311-post-266-rollup/lambda/api   # the shipped source
rm -rf /tmp/api-build && mkdir -p /tmp/api-build
pip install --no-cache-dir \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --upgrade -r requirements.txt -t /tmp/api-build
cp -a . /tmp/api-build/
( cd /tmp/api-build && zip -qr /tmp/api-lambda.zip . )
cd -
# sanity: the zip must contain the deps, not just *.py
unzip -l /tmp/api-lambda.zip | grep -qi 'aws_lambda_powertools' && echo "deps bundled OK" || { echo "MISSING DEPS — stop"; exit 1; }

# 2. Update BOTH functions that run this code (fixed names — confirm they exist first).
for FN in openclaw-api openclaw-lifecycle-consumer; do
  aws lambda get-function --function-name "$FN" --region <region> >/dev/null 2>&1 \
    || { echo "SKIP $FN (not present in this deployment)"; continue; }
  aws lambda update-function-code --function-name "$FN" \
    --zip-file fileb:///tmp/api-lambda.zip --region <region> --publish
done

# 3. Point the `live` alias at the API's newly published version (API GW uses this alias).
#    The consumer is triggered on $LATEST by its SQS event source — no alias to move.
NEW_VER=$(aws lambda publish-version --function-name openclaw-api --region <region> \
  --query Version --output text)
echo "publishing live -> version $NEW_VER  (rollback point was $ROLLBACK_VER)"
aws lambda update-alias --function-name openclaw-api --name live \
  --function-version "$NEW_VER" --region <region>

# 4. IMMEDIATE post-apply check (before the slower business-path verify below): confirm the
#    live alias now serves the new version AND the new code contains the #298/#314 markers.
LIVE_NOW=$(aws lambda get-alias --function-name openclaw-api --name live \
  --region <region> --query FunctionVersion --output text)
[ "$LIVE_NOW" = "$NEW_VER" ] && echo "live -> $LIVE_NOW OK" || echo "WARN: live=$LIVE_NOW != $NEW_VER"
# the shipped source must carry the fixes (proves you packaged the patched tree, not the old one):
grep -q "_resolve_proxy_route" handler.py && grep -q "persist_device_paired_b64" services/tenant_service.py \
  && echo "packaged source has #298 + #314 markers OK" || echo "STOP: packaged source is missing the fixes"
```

> This updates code only; it does **not** apply the IAM grant or the VPCE. Those are separate
> layers — do `iam/apply-iam.sh` first (fail-closed prerequisite) and handle the VPCE via
> `network/APPLY-NETWORK.md`.
>
> **Rollback (instant, no rebuild — uses the Step-0 rollback point):**
>
> - `openclaw-api`: `aws lambda update-alias --function-name openclaw-api --name live
--function-version "$ROLLBACK_VER" --region <region>` — flips the alias back to exactly what
>   was serving before; the bad version is left published but unreferenced.
> - `openclaw-lifecycle-consumer`: it runs on `$LATEST`, so there is no alias to flip. Roll back
>   by re-deploying a zip built from the pre-patch source (a `base_sha` checkout) with the same
>   Step-1 bundling, then `update-function-code`. Confirm with `get-function ... CodeSha256`
>   against `/tmp/consumer-codesha-before.txt`.

## Verify

- **Private-API routing:** on a private API, `curl` a non-`/ping` route (e.g.
  `GET /tenants`); it should respond instead of returning 404.
- **Rebuild semantics:** rebuild a tenant (the image-upgrade path) and confirm
  (1) the existing data disk is preserved, and (2) the version number updates only
  after the VM actually boots the new rootfs.
- **Paired-device persistence (#312/#314):** after `POST /tenants`, confirm the tenants
  table row for the new tenant has a non-empty `device_paired_b64` field
  (`aws dynamodb get-item --table-name <tenants> --key '{"id":{"S":"<tid>"}}'
--projection-expression device_paired_b64`). Then exercise the recovery path (rebuild /
  image update) and confirm the VM comes back **paired** (frontend not stuck on
  `NOT_PAIRED`) — the host re-injects `paired.json` from this field on relaunch.
