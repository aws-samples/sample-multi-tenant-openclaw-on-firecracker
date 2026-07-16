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

### 2a. PREREQUISITE — verify host role can read tenant-secrets (do this FIRST)

The patch is fail-closed: if positions 12/13 are empty it reads DDB, and if
that read is denied the launch ABORTS. Patching before fixing IAM turns
"token drift" into "VM won't start at all". Check on the host:

```bash
aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{"tenant_id":{"S":"__probe__"}}' --region <region>
```

- Returns `{}` or empty → OK, proceed.
- Returns `AccessDeniedException` → grant first, then re-run the probe:

```bash
aws iam put-role-policy --role-name <host-role-name> \
  --policy-name patch-266-tenant-secrets-read \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"dynamodb:GetItem","Resource":"arn:aws:dynamodb:<region>:<account>:table/openclaw-tenant-secrets"}]}'
```

(Find the role name from the error message or:
`aws sts get-caller-identity` on the host.)

### 2b. Add TENANT_SECRETS_TABLE to /etc/platform.env

```bash
ssh -i <key> ubuntu@<host> 'grep -q TENANT_SECRETS_TABLE /etc/platform.env 2>/dev/null || echo "TENANT_SECRETS_TABLE=openclaw-tenant-secrets" | sudo tee -a /etc/platform.env'
```

### 2c. Patch launch-vm.sh (back up first, diff after)

If your deployment runs a CUSTOMIZED launch-vm.sh, do NOT blind-replace:
first diff `launch-vm.sh.patched` against the live file and confirm the ONLY
delta is the #266 block; if there are other differences, insert the block from
`launch-vm-ddb-fallback.sh` manually instead (right after the #199 fallback
block's closing `fi`, before the `# #41 — harden-config.sh` comment).

```bash
# Back up
ssh -i <key> ubuntu@<host> 'cp /home/ubuntu/launch-vm.sh /home/ubuntu/launch-vm.sh.bak.266'
# Replace
scp -i <key> patch/266-token-drift-fix/launch-vm.sh.patched ubuntu@<host>:/home/ubuntu/launch-vm.sh
# Guardrail: the diff vs backup must contain ONLY the #266 block — otherwise roll back
ssh -i <key> ubuntu@<host> 'diff /home/ubuntu/launch-vm.sh.bak.266 /home/ubuntu/launch-vm.sh; bash -n /home/ubuntu/launch-vm.sh && grep -c "266" /home/ubuntu/launch-vm.sh'
```

Rollback if anything looks wrong:

```bash
ssh -i <key> ubuntu@<host> 'cp /home/ubuntu/launch-vm.sh.bak.266 /home/ubuntu/launch-vm.sh'
```

## Step 3: Ensure Future Instances Are Fixed

New hosts download `launch-vm.sh` from the S3 assets bucket at boot (init-host.sh
does the pull). You must upload the patched file to the EXACT path init-host.sh
reads from — **do not guess the path, verify it first on the host**:

```bash
# Find the real S3 path this deployment pulls launch-vm.sh from:
grep -o 's3://[^ ]*launch-vm.sh' /var/log/openclaw-init.log
# Fallback if the log rotated:
grep -rn 's3 cp.*launch-vm' /var/lib/cloud/instance/scripts/ /tmp/init-host.sh 2>/dev/null
```

(In the public repo the path is `s3://<assets-bucket>/deployment/scripts/launch-vm.sh`,
but customized deployments may differ — always trust the grep output.)

Then upload and verify round-trip:

```bash
aws s3 cp patch/266-token-drift-fix/launch-vm.sh.patched <the-real-s3-path> --region <region>
# Verify: download it back and confirm it contains the fix
aws s3 cp <the-real-s3-path> /tmp/verify-launch-vm.sh --region <region>
grep -c "266" /tmp/verify-launch-vm.sh   # must be > 0
```

No CDK deploy required. Note: `/etc/platform.env` on FUTURE hosts will NOT
contain `TENANT_SECRETS_TABLE` (init-host.sh is baked into the Launch Template
as base64+gzip and can't be changed without CDK deploy) — this is fine, the
patched script defaults to `openclaw-tenant-secrets` when the variable is
absent. Record a follow-up: on the next CDK deploy, add TENANT_SECRETS_TABLE
to init-host.sh and the DynamoDB GetItem grant to the host role in the stack.

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

## Step 5: Verify the Fix (two paths, two different expected logs)

IMPORTANT: a NORMAL create goes through dispatch which passes positions 12/13
with values — it will log `using control-plane pre-minted gateway token` and
will NOT log "DDB fallback". The fallback log only appears on the RECOVERY
path (empty args). You must test BOTH:

### 5a. Normal path (positional args intact)

```bash
curl -X POST "https://<api-url>/tenants" -H "x-api-key: <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "patch-266-test"}'
# Wait ~30s, then on host:
journalctl --no-pager -n 100 | grep -E "pre-minted|266"
# Expected: "using control-plane pre-minted gateway token"
# Verify paired.json:
cat /data/firecracker-vms/patch-266-test/data-mount/.openclaw/devices/paired.json | jq .deviceId
```

### 5b. Recovery path (THE path this patch fixes)

Simulate a crash so host-agent relaunches with only 4 args:

```bash
# On host: find and kill the test tenant's firecracker process
pgrep -af 'firecracker.*patch-266-test'   # note the PID
kill <pid>
# Wait for host-agent to recover it (~30-60s), then:
journalctl --no-pager -n 100 | grep -E "DDB fallback|266"
# Expected BOTH lines:
#   "DDB fallback: got gateway_token_ct from openclaw-tenant-secrets (#266)"
#   "DDB fallback: got device_paired_b64 from openclaw-tenant-secrets (#266)"

# Final check: the recovered VM's token still matches DDB (no drift):
cat /data/firecracker-vms/patch-266-test/data-mount/.openclaw/openclaw.json | jq -r '.gateway.auth.token'
# Compare with the control-plane token via GET /tenants/patch-266-test/credentials
```

Clean up:

```bash
curl -X DELETE "https://<api-url>/tenants/patch-266-test" -H "x-api-key: <api-key>"
```
