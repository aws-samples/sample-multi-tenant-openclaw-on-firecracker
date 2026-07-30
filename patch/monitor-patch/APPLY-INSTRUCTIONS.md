# monitor-patch — Apply Instructions

**Source**: gateway branch `c6fafab` (base: `a547dc7`)
**Status**: MANUAL_REVIEW (new DDB table + new Lambda + LT change require operator review)
**CDK deploy**: FORBIDDEN — all changes applied via manual CLI equivalents.

## What this patch delivers

1. **#387 Host-agent Prometheus exporter** — app_health (gateway HTTP liveness per tenant),
   DNAT port watermark, build_info SHA, loop_tick heartbeat, ssm_agent liveness, route_failure
   counter. Closes the #197 blind spot.
2. **Scalable tenant queries** — paginated GET /tenants with AES-GCM cursor encryption,
   optional GSIs (gsi_host/gsi_status/gsi_rootfs_version). Feature-gated: `tenant_query.enabled`.
3. **Tenant stats aggregation** — new openclaw-tenant-stats-writer Lambda (1min schedule),
   GET /tenants-stats route. Feature-gated: `tenant_stats.enabled`.
4. **S3 user hooks (#390)** — optional customer-managed root hook at host init. Feature-gated:
   `user_hooks.host_init` in config.
5. **stop-vm lifecycle lock** — stop serializes with launch via flock(fd9), legacy detection
   for pre-patch VMs.

---

## Step 0 — DISCOVER (read-only probe)

```bash
# Run discover-env.sh to populate environment.json
bash lib/discover-env.sh
# Review environment.json: region, account, API URL, ASG, LT version, host IDs
cat environment.json
```

Confirm: region, account, API endpoint, hosts-ASG identity, current LT version.

---

## Step 1 — Backup (before any change)

```bash
REGION=$(jq -r .region environment.json)
API_FN="openclaw-api"
HC_FN="openclaw-health-check"

# 1a. Lambda: publish anchor versions
aws lambda publish-version --function-name $API_FN --region $REGION \
  --description "pre-monitor-patch anchor" | tee /tmp/api-anchor.json
aws lambda publish-version --function-name $HC_FN --region $REGION \
  --description "pre-monitor-patch anchor" | tee /tmp/hc-anchor.json

# 1b. Lambda: download live zips for rollback
aws lambda get-function --function-name $API_FN --region $REGION \
  --query 'Code.Location' --output text | xargs curl -o /tmp/api-backup.zip
aws lambda get-function --function-name $HC_FN --region $REGION \
  --query 'Code.Location' --output text | xargs curl -o /tmp/hc-backup.zip

# 1c. S3: record current script versions
BUCKET=$(jq -r .assets_bucket environment.json)
for script in host-agent.py launch-vm.sh stop-vm.sh migrate-vm.sh; do
  aws s3api head-object --bucket $BUCKET --key "deployment/scripts/$script" \
    --region $REGION --query 'VersionId' --output text > "/tmp/s3-ver-$script.txt"
done

# 1d. LT: record current version
LT_ID=$(jq -r .lt_id environment.json)
LT_VER=$(jq -r .lt_version environment.json)
echo "LT backup: $LT_ID version $LT_VER"
```

---

## Step 2 — Hot-fix running hosts (Layer B: S3-pulled scripts)

These scripts are pulled from S3 at boot and live at `/opt/openclaw/` on hosts.
Hot-fix = push to live hosts NOW + update S3 for future boots.

```bash
HOSTS=$(jq -r '.host_ids[]' environment.json)

# 2a. Push patched scripts to all live hosts
for HOST in $HOSTS; do
  for script in host-agent.py launch-vm.sh stop-vm.sh migrate-vm.sh; do
    # SSM send-command to write file
    aws ssm send-command --instance-ids "$HOST" --region $REGION \
      --document-name "AWS-RunShellScript" \
      --parameters "commands=[\"cat > /opt/openclaw/$script << 'SCRIPTEOF'\n$(cat host-scripts/${script}.patched)\nSCRIPTEOF\"]" \
      --output text --query 'Command.CommandId'
  done
done

# 2b. Restart host-agent on each host to pick up new metrics code
for HOST in $HOSTS; do
  aws ssm send-command --instance-ids "$HOST" --region $REGION \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["systemctl restart host-agent"]'
done

# 2c. Upload to S3 for future boots
for script in host-agent.py launch-vm.sh stop-vm.sh migrate-vm.sh; do
  aws s3 cp "host-scripts/${script}.patched" \
    "s3://$BUCKET/deployment/scripts/$script" --region $REGION
done
```

---

## Step 3 — Lambda overlay (Layer C)

### 3a. openclaw-api Lambda

```bash
# Download live package
mkdir -p /tmp/api-overlay && cd /tmp/api-overlay
cp /tmp/api-backup.zip ./live.zip
unzip -q live.zip -d live/

# Verify base hashes of files we're replacing (drift guard)
# If any mismatch: STOP — the live function has drifted from expected base
sha256sum live/handler.py  # must match manifest base_sha256

# Overlay patched source (ONLY the changed files)
cp <patch-dir>/lambda/api/handler.py live/handler.py
cp <patch-dir>/lambda/api/core/auth.py live/core/auth.py
cp <patch-dir>/lambda/api/core/clients.py live/core/clients.py
cp <patch-dir>/lambda/api/core/pagination.py live/core/pagination.py
cp <patch-dir>/lambda/api/services/fleet_service.py live/services/fleet_service.py
cp <patch-dir>/lambda/api/services/host_service.py live/services/host_service.py
cp <patch-dir>/lambda/api/services/registry_service.py live/services/registry_service.py
cp <patch-dir>/lambda/api/services/tenant_query_service.py live/services/tenant_query_service.py
cp <patch-dir>/lambda/api/services/tenant_service.py live/services/tenant_service.py
cp <patch-dir>/lambda/api/services/tenant_stats_service.py live/services/tenant_stats_service.py

# Re-zip and deploy
cd live && zip -qr ../patched.zip . && cd ..
REVISION=$(aws lambda get-function --function-name $API_FN --region $REGION \
  --query 'Configuration.RevisionId' --output text)
aws lambda update-function-code --function-name $API_FN --region $REGION \
  --zip-file fileb://patched.zip --revision-id $REVISION
aws lambda wait function-updated --function-name $API_FN --region $REGION

# Publish + update alias + verify
aws lambda publish-version --function-name $API_FN --region $REGION | tee /tmp/api-new-ver.json
NEW_VER=$(jq -r .Version /tmp/api-new-ver.json)
aws lambda update-alias --function-name $API_FN --name live --region $REGION \
  --function-version $NEW_VER
```

### 3b. openclaw-health-check Lambda

```bash
mkdir -p /tmp/hc-overlay && cd /tmp/hc-overlay
cp /tmp/hc-backup.zip ./live.zip
unzip -q live.zip -d live/
cp <patch-dir>/lambda/health_check/handler.py live/handler.py
cd live && zip -qr ../patched.zip . && cd ..
REVISION=$(aws lambda get-function --function-name $HC_FN --region $REGION \
  --query 'Configuration.RevisionId' --output text)
aws lambda update-function-code --function-name $HC_FN --region $REGION \
  --zip-file fileb://patched.zip --revision-id $REVISION
aws lambda wait function-updated --function-name $HC_FN --region $REGION
```

---

## Step 4 — CDK stack changes (NO cdk deploy)

### 4a. Lambda env vars (AUTO_CLI)

```bash
# Add TENANT_QUERY_ENABLED (default false — feature off until explicitly enabled)
aws lambda update-function-configuration --function-name $API_FN --region $REGION \
  --environment "Variables={$(aws lambda get-function-configuration --function-name $API_FN \
  --region $REGION --query 'Environment.Variables' --output json | \
  python3 -c 'import sys,json; d=json.load(sys.stdin); d["TENANT_QUERY_ENABLED"]="false"; print(",".join(f"{k}={v}" for k,v in d.items()))')}"
```

### 4b. GET /tenants-stats API route (AUTO_CLI)

```bash
API_ID=$(jq -r .api_id environment.json)
# Create /tenants-stats resource + GET method (clone auth from existing routes)
bash lib/apply-api-routes.sh apply $REGION
```

### 4c. MANUAL_REVIEW items (operator must review before executing)

The following require operator review:

1. **New DDB table `openclaw-tenant-stats`** (only if `tenant_stats.enabled=true`):
   ```bash
   aws dynamodb create-table \
     --table-name openclaw-tenant-stats \
     --attribute-definitions '[{"AttributeName":"id","AttributeType":"S"}]' \
     --key-schema '[{"AttributeName":"id","KeyType":"HASH"}]' \
     --billing-mode PAY_PER_REQUEST \
     --region $REGION
   aws dynamodb update-continuous-backups \
     --table-name openclaw-tenant-stats \
     --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
     --region $REGION
   ```

2. **New Secrets Manager secret `openclaw/pagination-cursor`** (only if `tenant_query.enabled=true`):
   ```bash
   aws secretsmanager create-secret \
     --name "openclaw/pagination-cursor" \
     --region $REGION \
     --secret-string '{"purpose":"pagination-aes-gcm","key":"'$(openssl rand -base64 32)'"}'
   # Then add PAGINATION_AES_KEY to the Lambda env:
   # (extract key value and add to update-function-configuration)
   ```

3. **New Lambda `openclaw-tenant-stats-writer`** (only if `tenant_stats.enabled=true`):
   ```bash
   # Create the function + EventBridge 1min schedule
   # See manifest operations for full parameters
   ```

4. **LT UserData update for {{HOST_USER_HOOK}}** (only if `user_hooks.host_init` configured):
   ```bash
   # Use lib/lt-userdata.py to decode current LT → patch → re-encode → new LT version
   # Then update ASG to pin new version + instance-refresh
   ```

5. **Optional GSIs on openclaw-tenants** (only if `scaler.add_gsi_*` enabled):
   ```bash
   # aws dynamodb update-table with GSI additions (one at a time, wait for ACTIVE)
   ```

---

## Step 5 — Edge layer (nginx + fluent-bit)

```bash
# Upload updated edge configs to S3
aws s3 cp deploy/edge/nginx.conf "s3://$BUCKET/deployment/edge/nginx.conf" --region $REGION
aws s3 cp deploy/edge/fluent-bit/host/fluent-bit.conf \
  "s3://$BUCKET/deployment/edge/fluent-bit/host/fluent-bit.conf" --region $REGION

# Reload on live edge instances (if edge is separate from hosts)
# aws ssm send-command to restart nginx/fluent-bit on edge nodes
```

---

## Step 6 — Guided verification

### Phase A — Read-only (always run)

| ID | Action | Pass |
|---|---|---|
| v-metrics-endpoint | `curl http://<host>:9200/metrics` | HTTP 200, all metric families present |
| v-port-watermark | grep `openclaw_host_dnat_ports` in metrics | used/total/quarantined present, used <= total |
| v-stats-route | `curl GET /tenants-stats` | HTTP 200 (or 404 if feature disabled — expected) |
| v-cursor-decrypt | Send invalid cursor to paginated endpoint | HTTP 400 (not 500) |
| v-hook-disabled-noop | Decode LT UserData, check no literal `{{` | No raw placeholder in rendered output |

### Phase B — Lifecycle (run once)

| ID | Action | Pass |
|---|---|---|
| v-app-health-gauge | Create tenant → running → check metrics | `openclaw_app_health{tenant=<id>} == 1` |
| v-paginated-list | GET /tenants?limit=2 | Response has next_cursor + correct page size |
| v-stats-writer-runs | Wait 2min → scan tenant-stats table | Count > 0 (if feature enabled) |
| v-stop-no-race | Launch + immediate stop (race) | Tenant stops cleanly, no kill errors |
| v-legacy-compat | Stop pre-patch tenant | Legacy detection log + clean stop |

---

## Step 7 — Teardown test tenants

```bash
# Delete ONLY the exact test tenant IDs created during verification
for TID in <exact-ids-from-verification>; do
  curl -X DELETE "$API_URL/tenants/$TID?keep_data=false" -H "x-api-key: $KEY"
done
# Confirm real tenant count unchanged
```

---

## Rollback

- **Lambda**: redeploy backup zip + revert alias to anchor version + update $LATEST
- **S3 scripts**: `aws s3api get-object --version-id <saved-version>` to restore each script
- **LT**: revert ASG to pin previous LT version
- **DDB table/GSI**: delete table (if newly created) or remove GSI
- **Secrets Manager**: delete secret (if newly created)
- **API route**: `bash lib/apply-api-routes.sh rollback $REGION`
