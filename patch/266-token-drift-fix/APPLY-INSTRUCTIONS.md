# AI Execution Instructions for Patch #266

You are an AI assistant helping apply a hotfix to a running OpenClaw on Firecracker
deployment. Follow these steps in order. Ask the user for required information
where indicated.

## Context

After deployment, tenants may fail to connect via WebSocket (JDWS) because:

- The gateway token in the VM doesn't match what the API returns
- The device paired.json is missing, requiring manual approve

Root cause: host-agent recovery relaunches VMs without passing the gateway token
and device identity from DynamoDB, causing a fallback to random token generation.

## Step 1: Gather Information

Ask the user:

```
1. What is the host IP (metal instance) and SSH key path?
2. What is the AWS region? (e.g., ap-southeast-1)
3. Are there existing tenants that are broken (can't connect)?
   If yes, what are their tenant_ids?
```

## Step 2: Fix the Current Instance (immediate)

SSH into the host and apply the two changes:

### 2a. Add TENANT_SECRETS_TABLE to /etc/platform.env

```bash
ssh -i <key> ubuntu@<host> 'grep -q TENANT_SECRETS_TABLE /etc/platform.env 2>/dev/null || echo "TENANT_SECRETS_TABLE=openclaw-tenant-secrets" | sudo tee -a /etc/platform.env'
```

### 2b. Patch launch-vm.sh

Copy the patched launch-vm.sh from this repository to the host:

```bash
scp -i <key> deploy/userdata/launch-vm.sh ubuntu@<host>:/home/ubuntu/launch-vm.sh
```

Verify:

```bash
ssh -i <key> ubuntu@<host> 'grep -c "266" /home/ubuntu/launch-vm.sh'
```

Expected: a number > 0 (the patch references are present).

## Step 3: Ensure Future Instances Are Fixed

The patched `deploy/userdata/launch-vm.sh` and `deploy/userdata/init-host.sh`
are already committed to this repository. If the deployment pulls from this repo
(e.g., via S3 assets bucket or CodeBuild), update the S3 copy:

```bash
aws s3 cp deploy/userdata/launch-vm.sh s3://<assets-bucket>/userdata/launch-vm.sh --region <region>
aws s3 cp deploy/userdata/init-host.sh s3://<assets-bucket>/userdata/init-host.sh --region <region>
```

If using a Launch Template that references S3 userdata, new instances will
automatically pick up the fix. No CDK deploy required.

## Step 4: Handle Broken Tenants

For each broken tenant_id, ask the user:

```
Tenant <tenant_id> has a token mismatch. Options:
  A) Rebuild the tenant (DELETE + re-create) — simplest, loses in-VM state
  B) Keep the tenant — I'll skip it, you can fix manually later

Which do you prefer?
```

### If user chooses A (rebuild):

```bash
# Delete
curl -X DELETE "https://<api-url>/tenants/<tenant_id>" -H "x-api-key: <api-key>"

# Wait for deletion to complete (~12s)
sleep 15

# Re-create with same parameters
curl -X POST "https://<api-url>/tenants" -H "x-api-key: <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "<tenant_id>"}'
```

### If user chooses B (skip):

Note it and move on. The tenant will continue to have connection issues until
manually rebuilt or token-synced.

## Step 5: Verify the Fix

After patching, create a test tenant to verify:

```bash
# Create a test tenant
curl -X POST "https://<api-url>/tenants" -H "x-api-key: <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "patch-266-test"}'
```

Wait ~30s for the VM to launch, then check:

```bash
# On host: check launch logs
ssh -i <key> ubuntu@<host> 'journalctl --no-pager -n 50 | grep -E "266|DDB fallback|pre-minted"'

# On host: verify paired.json exists
ssh -i <key> ubuntu@<host> 'cat /data/firecracker-vms/patch-266-test/data-mount/.openclaw/devices/paired.json 2>/dev/null | jq .deviceId'
```

Expected:

- Launch logs show "DDB fallback: got gateway_token_ct" and "got device_paired_b64"
- paired.json contains a valid deviceId

Clean up:

```bash
curl -X DELETE "https://<api-url>/tenants/patch-266-test" -H "x-api-key: <api-key>"
```

## IAM Prerequisite Check

The host instance role needs `dynamodb:GetItem` on `openclaw-tenant-secrets`.
Verify:

```bash
ssh -i <key> ubuntu@<host> 'aws dynamodb get-item --table-name openclaw-tenant-secrets --key "{\"tenant_id\":{\"S\":\"__test__\"}}" --region <region> 2>&1 | head -3'
```

- If it returns `{}` or an empty Item → permission OK
- If it returns `AccessDeniedException` → add the permission to the host role:
  ```
  Table ARN: arn:aws:dynamodb:<region>:<account>:table/openclaw-tenant-secrets
  Action: dynamodb:GetItem
  ```
