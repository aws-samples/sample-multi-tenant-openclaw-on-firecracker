# Patch #266: Gateway Token Drift Fix

## Problem

When a microVM is relaunched by host-agent recovery (e.g., after a failed first
launch or health-check triggered rebuild), the recovery path only passes 4
positional arguments to `launch-vm.sh`. Positions 12 (`gateway_token_ct`) and 13
(`device_paired_b64`) are empty.

If the data disk is freshly created (`NEW_DATA=true`), two things go wrong:

1. **Token mismatch**: The token injection block falls back to `openssl rand -hex 24`,
   generating a random token the control plane doesn't know about. The API returns
   the DDB-stored token (A), but the VM has token (B) → WebSocket connection refused.

2. **No auto-pair**: The `devices/paired.json` injection is skipped entirely → the
   gateway requires manual device approval even though the control plane already
   minted a device keypair for the tenant.

## Root Cause

`host-agent.py` `_recover_vm()` / `_force_relaunch_vm()` invoke launch-vm.sh
with only `(tenant_id, vm_num, vcpu, mem_mb)` — positions 12/13 are never passed.

## Fix

Make `launch-vm.sh` self-serve from DynamoDB `openclaw-tenant-secrets` table when
positions 12/13 are empty, using the same pattern as the existing #199 fix for
`restore_backup_key`/`config_template`. Fail-closed: if the DDB read fails, the
launch aborts (scheduler retries).

## Prerequisites

- Host instance role must have `dynamodb:GetItem` on `openclaw-tenant-secrets` table.
  (This is already granted if the host can read from `openclaw-tenants` — check your
  CDK stack's host role grants. If not, add the permission manually.)
- `jq` must be installed on the host (already present from init-host.sh).
- The tenant must have been created via the control plane API (which mints
  `gateway_token_ct` and `device_paired_b64` into `openclaw-tenant-secrets`).

## How to Apply

### Step 1: Add TENANT_SECRETS_TABLE to platform.env

SSH into the host and run:

```bash
sudo bash /path/to/apply-platform-env.sh
```

Or manually:

```bash
echo "TENANT_SECRETS_TABLE=openclaw-tenant-secrets" >> /etc/platform.env
```

### Step 2: Patch launch-vm.sh

Open `/home/ubuntu/launch-vm.sh` on the host. Find this line (around line 459):

```
  fi
fi
# #41 — harden-config.sh ...
```

Insert the contents of `launch-vm-ddb-fallback.sh` between the `fi` (end of the
#199 fix block) and the `# #41` comment. The result should be:

```
  fi
fi
# #266 fix: host-agent _recover_vm / _force_relaunch_vm ...
if [ -z "${INJECTED_GATEWAY_TOKEN_CT}" ] || [ -z "${INJECTED_DEVICE_PAIRED_B64}" ]; then
  ...
fi
# #41 — harden-config.sh ...
```

Or replace the entire `launch-vm.sh` with the pre-patched version included in
this patch directory:

```bash
scp -i <key> patch/266-token-drift-fix/launch-vm.sh.patched ubuntu@<host>:/home/ubuntu/launch-vm.sh
```

### Step 3: Fix Existing Affected Tenants

Tenants already launched with a mismatched token need to be rebuilt:

```bash
# Option A: Rebuild (simplest, no data loss if backup exists)
curl -X DELETE "https://<api>/tenants/<tenant_id>" -H "x-api-key: <key>"
curl -X POST "https://<api>/tenants" -H "x-api-key: <key>" \
  -d '{"tenant_id": "<tenant_id>", ...}'

# Option B: Read VM's actual token and sync back to DDB (advanced)
# 1. Read the token the VM is actually using:
ssh ubuntu@<host> "cat /data/firecracker-vms/<tid>/data-mount/.openclaw/openclaw.json" \
  | jq -r '.gateway.auth.token'
# 2. KMS-encrypt it with the correct EncryptionContext and update DDB.
#    (See tenant_service.py mint_gateway_token for the exact encrypt call.)
```

## Verification

After applying the patch, create a new tenant (or rebuild an existing one) and
verify:

### 1. Check launch logs for the DDB fallback message

```bash
# On the host, check the latest launch log:
journalctl -u launch-vm@<tenant_id> --no-pager | grep -i "266\|DDB fallback\|pre-minted"
```

Expected output (one or both):

```
DDB fallback: got gateway_token_ct from openclaw-tenant-secrets (#266)
DDB fallback: got device_paired_b64 from openclaw-tenant-secrets (#266)
using control-plane pre-minted gateway token (reveal-capable, #187 P1)
```

### 2. Verify token consistency

```bash
# A) Read what the VM is using:
VM_TOKEN=$(ssh ubuntu@<host> "cat /data/firecracker-vms/<tid>/data-mount/.openclaw/openclaw.json" \
  | jq -r '.gateway.auth.token')

# B) Read what the API returns (decrypt the enc:v1 envelope with your private key):
API_TOKEN=$(curl -s "https://<api>/tenants/<tid>/credentials" \
  -H "x-api-key: <key>" | jq -r '.gateway_token')
# Decrypt API_TOKEN with your RSA private key...

# C) They must match:
echo "VM=$VM_TOKEN"
echo "API=(decrypted value)"
```

### 3. Verify device auto-pairing (no manual approve)

```bash
# Connect with JDWS using the pre-provisioned device identity.
# Expected: WebSocket connects successfully, no "approve" prompt in gateway logs.
# Check paired.json exists in the VM:
ssh ubuntu@<host> "cat /data/firecracker-vms/<tid>/data-mount/.openclaw/devices/paired.json" | jq .
```

The file should contain the device entry with `deviceId`, `publicKey`, `roles`.

## Files

| File                        | Purpose                                                            |
| --------------------------- | ------------------------------------------------------------------ |
| `launch-vm.sh.patched`      | Complete launch-vm.sh with fix applied — direct replacement        |
| `launch-vm-ddb-fallback.sh` | The code block to insert (if you prefer manual patching)           |
| `apply-platform-env.sh`     | Idempotent script to add TENANT_SECRETS_TABLE to /etc/platform.env |
| `APPLY-INSTRUCTIONS.md`     | Step-by-step AI-executable instructions for applying this fix      |
| `README.md`                 | This file                                                          |
