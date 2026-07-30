# monitor-patch — Apply Instructions

**Source**: gateway branch `c6fafab` (base: `a547dc7`)
**Status**: MANUAL_REVIEW (new DDB table + new Lambda + LT change require operator review)
**CDK deploy**: FORBIDDEN — all changes applied via manual CLI equivalents.

## What this patch delivers

1. **#387 Host-agent Prometheus exporter** — app_health (gateway HTTP liveness per tenant),
   DNAT port watermark, build_info SHA, loop_tick heartbeat, ssm_agent liveness, route_failure
   counter. Closes the #197 blind spot.
2. **Scalable tenant queries** — Feature-gated by `tenant_query.enabled` (default OFF).
   IMPORTANT cursor semantics: the **default** `GET /tenants` (no new params) keeps the
   LEGACY cursor — `base64(JSON(LastEvaluatedKey))`, plaintext, tenant ids readable
   (utils.py:_encode_next_token). **AES-GCM encrypted cursors apply ONLY to** `GET /hosts`
   pagination and the new single-condition query paths (`?user_id=|host_id=|status=|
   rootfs_version=`) once the flag is on (core/pagination.py, used only by host_service.py +
   tenant_query_service.py). Those query paths ALSO require 4 GSIs on openclaw-tenants —
   gsi_tenant_user / gsi_host / gsi_status / gsi_rootfs_version — see item 5 caveat below;
   without them the query returns 503 UNAVAILABLE (fail-closed, not a crash).
3. **Tenant stats aggregation** — new openclaw-tenant-stats-writer Lambda (1min schedule),
   GET /tenants-stats route. Feature-gated: `tenant_stats.enabled`.
4. **S3 user hooks (#390)** — optional customer-managed root hook at host init. Feature-gated:
   `user_hooks.host_init` in config.
5. **stop-vm lifecycle lock** — stop serializes with launch via flock(fd9), legacy detection
   for pre-patch VMs.

---

## Step 0 — DISCOVER (read-only probe)

```bash
# discover-env.sh REQUIRES the target region as its first arg (it exits 2 without it).
# This is the one value the operator supplies by hand; everything else is discovered.
REGION=<your-deploy-region>     # e.g. ap-southeast-1
bash lib/discover-env.sh "$REGION"
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

### 4b. GET /tenants-stats API route (AUTO_CLI) — only if `tenant_stats.enabled=true`

NOTE: do NOT use `lib/apply-api-routes.sh` here — the copy shipped in this kit is
patch 376's (it builds `POST /create-image-snapshot` cloned from `GET /images`, and
takes `plan|apply|verify|rollback <rest-api-id> <stage> <region>`). This patch needs
`GET /tenants-stats`, a root resource with the same auth as `GET /tenants`
(lambdas.py:1024-1025: method GET, AWS_PROXY to the api Lambda, api-key required).
Clone it inline:
```bash
API_ID=$(jq -r .api_id environment.json)
STAGE=$(jq -r .api_stage environment.json)     # e.g. v1
ROOT_ID=$(aws apigateway get-resources --rest-api-id "$API_ID" --region "$REGION" \
  --query "items[?path=='/'].id" --output text)
# reuse the exact AWS_PROXY integration URI + auth of the existing GET /tenants
TENANTS_ID=$(aws apigateway get-resources --rest-api-id "$API_ID" --region "$REGION" \
  --query "items[?path=='/tenants'].id" --output text)
INT_URI=$(aws apigateway get-integration --rest-api-id "$API_ID" --region "$REGION" \
  --resource-id "$TENANTS_ID" --http-method GET --query uri --output text)
AUTH_TYPE=$(aws apigateway get-method --rest-api-id "$API_ID" --region "$REGION" \
  --resource-id "$TENANTS_ID" --http-method GET --query authorizationType --output text)
AUTHZ_ID=$(aws apigateway get-method --rest-api-id "$API_ID" --region "$REGION" \
  --resource-id "$TENANTS_ID" --http-method GET --query authorizerId --output text 2>/dev/null)
# create /tenants-stats resource + GET method + integration, matching GET /tenants
STATS_ID=$(aws apigateway create-resource --rest-api-id "$API_ID" --region "$REGION" \
  --parent-id "$ROOT_ID" --path-part tenants-stats --query id --output text)
aws apigateway put-method --rest-api-id "$API_ID" --region "$REGION" \
  --resource-id "$STATS_ID" --http-method GET \
  --authorization-type "$AUTH_TYPE" ${AUTHZ_ID:+--authorizer-id "$AUTHZ_ID"} \
  --api-key-required
aws apigateway put-integration --rest-api-id "$API_ID" --region "$REGION" \
  --resource-id "$STATS_ID" --http-method GET \
  --type AWS_PROXY --integration-http-method POST --uri "$INT_URI"
# redeploy the stage so the new route goes live
aws apigateway create-deployment --rest-api-id "$API_ID" --region "$REGION" --stage-name "$STAGE"
# verify (private API: run from inside the VPC with the API key; 503 = feature not
# yet configured/no snapshot, still proves the route resolves — NOT 403/404):
# curl -s -o /dev/null -w '%{http_code}' -H "x-api-key: <key>" "$CTRL_API_BASE/tenants-stats"
```

### 4c. MANUAL_CLI_REVIEW items (operator reviews, then runs the exact CLI)

Every command below is complete and executable — no "see manifest" placeholders.
`$REGION`/`$ACCT`/`$API_FN`/`$BUCKET` come from `environment.json` (Step 0). Each item is
feature-gated: run it ONLY if the paired config flag is on. **Ordering matters** —
item 2 (pagination key) MUST complete before item 6 flips `TENANT_QUERY_ENABLED=true`,
or the first `GET /tenants?...` 500s on a missing key (fail-closed by design).

**1. New DDB table `openclaw-tenant-stats`** — only if `tenant_stats.enabled=true`.
The stats-writer publishes its snapshot here; `GET /tenants-stats` reads it.
```bash
aws dynamodb create-table \
  --table-name openclaw-tenant-stats \
  --attribute-definitions '[{"AttributeName":"id","AttributeType":"S"}]' \
  --key-schema '[{"AttributeName":"id","KeyType":"HASH"}]' \
  --billing-mode PAY_PER_REQUEST --region "$REGION"
aws dynamodb wait table-exists --table-name openclaw-tenant-stats --region "$REGION"
aws dynamodb update-continuous-backups --table-name openclaw-tenant-stats \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true --region "$REGION"
```

**2. Pagination cursor key** — only if `tenant_query.enabled=true`. **Do this BEFORE item 6.**
The API base64url-decodes `PAGINATION_AES_KEY` and requires **exactly 32 bytes**
(`pagination.py:_key()`). The CDK generates a 43-char URL-safe alnum string
(`password_length=43, exclude_punctuation`), NOT `openssl rand -base64 32` (that is
standard base64 with `+/=` — it mis-decodes and 500s). Generate a matching key:
```bash
# 43 URL-safe alnum chars == 32 bytes after base64url-decode (matches the CDK generator)
PAGKEY=$(python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(43)))")
aws secretsmanager create-secret --name "openclaw/pagination-cursor" --region "$REGION" \
  --secret-string "{\"purpose\":\"pagination-aes-gcm\",\"key\":\"$PAGKEY\"}"
# Inject as a plaintext env var (the CDK injects the resolved value, not a secret ref):
aws lambda get-function-configuration --function-name "$API_FN" --region "$REGION" \
  --query 'Environment.Variables' --output json > /tmp/api-env.json
python3 - "$PAGKEY" <<'PY' > /tmp/api-env-new.json
import json,sys
d=json.load(open("/tmp/api-env.json")); d["PAGINATION_AES_KEY"]=sys.argv[1]
print("Variables={%s}" % ",".join(f"{k}={v}" for k,v in d.items()))
PY
aws lambda update-function-configuration --function-name "$API_FN" --region "$REGION" \
  --environment "$(cat /tmp/api-env-new.json)"
aws lambda wait function-updated --function-name "$API_FN" --region "$REGION"
```
Rollback: `delete-secret --force-delete-without-recovery` + remove the env key.

**3. New Lambda `openclaw-tenant-stats-writer` + EventBridge 1-min schedule** — only if
`tenant_stats.enabled=true`. Reserved concurrency 1 (the CDK pins it so overlapping
minute-ticks cannot double-scan). First its execution role, then the function, then the rule.
```bash
# 3.1 execution role: scan tenants (read) + read/write the stats table + read assets manifest + logs
cat > /tmp/stats-writer-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
aws iam create-role --role-name openclaw-tenant-stats-writer-role \
  --assume-role-policy-document file:///tmp/stats-writer-trust.json
aws iam attach-role-policy --role-name openclaw-tenant-stats-writer-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
cat > /tmp/stats-writer-inline.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["dynamodb:Scan","dynamodb:DescribeTable"],"Resource":"arn:aws:dynamodb:$REGION:$ACCT:table/openclaw-tenants"},
 {"Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:PutItem","dynamodb:Query"],"Resource":"arn:aws:dynamodb:$REGION:$ACCT:table/openclaw-tenant-stats"},
 {"Effect":"Allow","Action":["s3:GetObject"],"Resource":"arn:aws:s3:::$BUCKET/*"}
]}
JSON
aws iam put-role-policy --role-name openclaw-tenant-stats-writer-role \
  --policy-name stats-writer --policy-document file:///tmp/stats-writer-inline.json
sleep 10   # let the role propagate before create-function

# 3.2 package + create the function (source shipped in this patch: lambda/tenant_stats/)
( cd lambda/tenant_stats && zip -qr /tmp/tenant-stats-writer.zip . )
aws lambda create-function --function-name openclaw-tenant-stats-writer --region "$REGION" \
  --runtime python3.12 --architectures arm64 --handler handler.lambda_handler \
  --timeout 50 --memory-size 8192 \
  --role "arn:aws:iam::$ACCT:role/openclaw-tenant-stats-writer-role" \
  --zip-file fileb:///tmp/tenant-stats-writer.zip \
  --environment "Variables={TENANTS_TABLE=openclaw-tenants,TENANT_STATS_TABLE=openclaw-tenant-stats,ASSETS_BUCKET=$BUCKET,ROOTFS_PREFIX=deployment/rootfs,STATS_SCAN_SEGMENTS=8}"
aws lambda wait function-active --function-name openclaw-tenant-stats-writer --region "$REGION"
# pin reserved concurrency 1 (no overlapping scans)
aws lambda put-function-concurrency --function-name openclaw-tenant-stats-writer \
  --reserved-concurrent-executions 1 --region "$REGION"

# 3.3 EventBridge rate(1 minute) -> the function
aws events put-rule --name openclaw-tenant-stats-schedule --region "$REGION" \
  --schedule-expression "rate(1 minute)" --state ENABLED
aws lambda add-permission --function-name openclaw-tenant-stats-writer --region "$REGION" \
  --statement-id tenant-stats-events --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:$REGION:$ACCT:rule/openclaw-tenant-stats-schedule"
aws events put-targets --rule openclaw-tenant-stats-schedule --region "$REGION" \
  --targets "Id=stats-writer,Arn=arn:aws:lambda:$REGION:$ACCT:function:openclaw-tenant-stats-writer"
# verify one tick lands a snapshot (wait ~70s for the first fire):
sleep 70
aws dynamodb scan --table-name openclaw-tenant-stats --region "$REGION" --max-items 1 \
  --query 'Count' --output text   # expect >= 1
```
Rollback: `events remove-targets` + `events delete-rule` + `lambda delete-function` +
`iam delete-role-policy`/`delete-role`. The `openclaw-tenant-stats` table is RETAIN
(stateful) — delete only if abandoning the feature.

**4. LT UserData update for the `{{HOST_USER_HOOK}}` block** — only if `user_hooks.host_init`
is configured. This changes what FUTURE hosts run, so it edits the Launch Template UserData.
⚠️ **This monitoring kit does NOT ship the LT re-bake tooling** (`lib/apply-lt.sh` +
`lib/lt-userdata.py`) — only `discover-env.sh` and `apply-api-routes.sh` are in `lib/`.
The LT UserData is baked (gzip+base64, CDK-exact, refuses any template still containing
`{{ }}`), so it must NOT be hand-edited. If you need the host user-hook on future hosts:
- the shipped `launch-template/init-host.sh.patched` is the new rendered source of truth;
- perform the LT roll with the `apply-lt.sh`/`lt-userdata.py` tools from an LT-touching
  patch kit (e.g. patch 315), which: decode the CURRENT LT version's already-rendered
  UserData → apply only the changed hunk → re-encode → publish a new immutable LT version →
  MIP-safe ASG instance-refresh, validating one new host (no `{{}}` in decoded UserData;
  registers into `openclaw-hosts`; ASG lifecycle CONTINUE) before rolling the fleet.
Running hosts are unaffected until they are replaced. Skip this item entirely if
`user_hooks.host_init` is not set (the default).

**5. GSIs on `openclaw-tenants` — MANDATORY foundation for the query feature (item 6).**
`tenant_query_service.py:18-21` maps the four query params to four indexes:
`user_id→gsi_tenant_user`, `host_id→gsi_host`, `status→gsi_status`,
`rootfs_version→gsi_rootfs_version`. **A stock deployment has only `gsi_owner`**
(storage.py builds `gsi_owner` always + `gsi_tenant_user` only when
`scaler.add_gsi_tenant_user=true`). The CDK does **NOT define gsi_host / gsi_status /
gsi_rootfs_version at all** — so `?host_id=|status=|rootfs_version=` return **503
UNAVAILABLE** ("index is not active") until you create them here. Create all four,
one at a time (DDB builds one GSI per update-table; each must reach ACTIVE first):
```bash
create_gsi () {  # $1=index  $2=attr_name
  aws dynamodb update-table --table-name openclaw-tenants --region "$REGION" \
    --attribute-definitions "AttributeName=$2,AttributeType=S" \
    --global-secondary-index-updates "[{\"Create\":{\"IndexName\":\"$1\",\"KeySchema\":[{\"AttributeName\":\"$2\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}}}]"
  echo "waiting for $1 ACTIVE..."
  until [ "$(aws dynamodb describe-table --table-name openclaw-tenants --region "$REGION" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='$1'].IndexStatus" --output text)" = "ACTIVE" ]; do sleep 20; done
}
create_gsi gsi_tenant_user     tenant_user_id
create_gsi gsi_host            host_id
create_gsi gsi_status          status
create_gsi gsi_rootfs_version  q_rootfs_version   # attr name is q_rootfs_version (tenant_query_service.py:21)
```
Note the `status` attribute: back-filling a GSI hash key requires every tenant row to
carry a non-empty `status`/`host_id`/`q_rootfs_version` attribute, or that row is simply
absent from the index (DDB sparse-index semantics) — acceptable for a query index, but
means the counts from these queries are over indexed rows only. Rollback: `update-table
--global-secondary-index-updates '[{"Delete":{"IndexName":"..."}}]'` per index.

**6. Enable the query feature flag** — only if `tenant_query.enabled=true`, and ONLY after
item 2 completed. This is the switch that makes `GET /tenants?user_id=|host_id=|status=`
live; keeping it last means the key exists before the first query decodes a cursor.
```bash
aws lambda get-function-configuration --function-name "$API_FN" --region "$REGION" \
  --query 'Environment.Variables' --output json > /tmp/api-env.json
python3 - <<'PY' > /tmp/api-env-on.json
import json
d=json.load(open("/tmp/api-env.json")); d["TENANT_QUERY_ENABLED"]="true"
print("Variables={%s}" % ",".join(f"{k}={v}" for k,v in d.items()))
PY
aws lambda update-function-configuration --function-name "$API_FN" --region "$REGION" \
  --environment "$(cat /tmp/api-env-on.json)"
aws lambda wait function-updated --function-name "$API_FN" --region "$REGION"
```
Rollback: set `TENANT_QUERY_ENABLED=false` (no-param `GET /tenants` is byte-identical either way).

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
| v-metrics-endpoint | `curl http://<host>:8899/metrics` | HTTP 200, all metric families present (host-agent binds OC_AGENT_PORT=8899, NOT 9200) |
| v-port-watermark | grep `openclaw_host_dnat_ports` in metrics | used/total/quarantined present, used <= total. **Port-range semantics (a real host carries TWO DNAT rule families — do not conflate them):** (1) the LIVE data-plane rule uses `--dport ∈ [10000,15000]` (route_ops.PortBitmap, allocated at promote, published to Redis, the port the edge actually dials; SG admits only this range, ha_edge.py:1423-1426). This gauge counts exactly this `--dport`. (2) a DEAD rule the control-plane adds at launch (`ssm_dispatch.py:143`) uses `--dport = 18789+vm_num` → `--to-destination guest:18789`; it is never dialed (edge uses the bitmap port) and SG-blocked, but it IS visible in `iptables -t nat -S PREROUTING`. So a real host shows both `--dport 10000-15000` (counted, live) and `--dport 18789+` (uncounted, dead) rules — if you `grep 18789` you see the dead family + every rule's `--to-destination :18789`. The gauge deliberately counts only the live [10000,15000] `--dport` (bitmap + edge SG + this gauge read the same `edge.dnat_port_{low,high}`, they never split). |
| v-stats-route | `curl GET /tenants-stats` | HTTP 200 with snapshot; HTTP **503 UNAVAILABLE** if tenant_stats not configured or no snapshot yet (expected, NOT a failure — tenant_stats_service.py returns 503, never 404) |
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

## Known limitations (not fixed by this patch — data-plane code, separate issue)

- **Residual PREROUTING DNAT rules after tenant delete.** The control-plane delete path
  removes the LIVE bitmap rule (tenant_service.py:2061-2067 `_dnat_remove_all_cmd` via SSM,
  gated on `if host_id`; the `:2056` comment shows this is the fix for a historical
  `-i <iface>`-prefix leak). But three residue paths remain, so a host with zero live VMs can
  still show DNAT rules — the real-machine "7 residual rules" observation is genuine:
  (a) the Scheme-A launch rule `--dport 18789+vm_num` (`ssm_dispatch.py:143`) is **never
  removed** — at promote the DDB `host_port` was overwritten to the bitmap value, so delete's
  `_dnat_remove_all_cmd` targets only the bitmap rule and the 18789+ rule leaks permanently
  (dead + uncounted, but present); (b) delete's SSM removal is best-effort/unchecked
  (`:2064`) — an SSM timeout silently leaves the bitmap rule; (c) the in-memory bitmap slot
  is never freed on delete (no `release-route` call in tenant_service — grep-confirmed), so
  the LIVE gauge over-counts deleted tenants until the agent restarts and rebuilds from
  iptables. The orphan-reap/drift backstops do NOT remove DNAT (host-agent.py:1662 passes
  `host_port=None`; drift is report-only). This is a data-plane route-lifecycle bug, NOT
  introduced by this monitoring patch — tracked separately, must not block this patch.
  Manual sweep on a host with zero live VMs: for each `--dport 10000-15000` PREROUTING rule
  with no matching `openclaw-tenants` row carrying that `host_port`, `-D` it; the `--dport
  18789+` rules are always safe to remove (never dialed).

## Rollback

- **Lambda**: redeploy backup zip + revert alias to anchor version + update $LATEST
- **S3 scripts**: `aws s3api get-object --version-id <saved-version>` to restore each script
- **LT**: revert ASG to pin previous LT version
- **DDB table/GSI**: delete table (if newly created) or remove GSI
- **Secrets Manager**: delete secret (if newly created)
- **API route**: delete the added resource (removes its GET method + integration) and
  redeploy the stage:
  ```bash
  aws apigateway delete-resource --rest-api-id "$API_ID" --region "$REGION" --resource-id "$STATS_ID"
  aws apigateway create-deployment --rest-api-id "$API_ID" --region "$REGION" --stage-name "$STAGE"
  ```
