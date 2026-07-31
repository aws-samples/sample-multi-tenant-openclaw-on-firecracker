# Applying `monitor-patch`

Source range:

- Base: `a547dc74fe25ea0219c804933c5a7da8af1e3b39`
- Patch: `74406d30749d91ecd98b075cc04aedc11585264b`
- Status: `MANUAL_REVIEW`

This is an environment-independent runbook for a human or customer-side coding
agent. Discover every resource in the customer account. Do not reuse resource
IDs, instance IDs, API keys, aliases, state files, or observations from another
environment.

The customer-side agent needs only this `patch/monitor-patch` directory from the
same Git revision. It must not call anything under `lib/`: those scripts are
operator/test helpers and are not part of this workflow. Every shell block in
this runbook requires Bash; zsh is not supported because names such as `path`
and `status` are special there, and the workflow uses Bash arrays and process
substitution. Start an interactive Bash, enter the patch directory, and verify
the required tools and artifacts:

```bash
command -v bash
test -n "${BASH_VERSION:-}" || {
  echo "FATAL: start bash before running this runbook" >&2
  exit 2
}
export PATCH_DIR="$(pwd -P)"
test -f "$PATCH_DIR/manifest.json"
command -v aws jq curl zip unzip sha256sum python3
aws sts get-caller-identity
```

Use AWS CLI and SSM only. Do not run CDK or `setup.sh`: either can overwrite
customer changes made after the original deployment. No local Docker is needed.
If monitoring is enabled, Docker commands run only on the remote monitoring node.

For every write: print the exact command, capture the pre-state and rollback
command, ask the terminal user to type `APPLY`, then execute and verify.

The supported customer predecessor is the layered deployment
`353-secret-ttl-plus-post315-rollup` followed by
`376-create-image-snapshot`. Patch 353 targets
`6edbf2abbfa4069bab8ee385a0b5cb3f22f57543`; patch 376 targets
`12ad3e5cff6aa13d253535ba421e51a73b417eda`. All 23 shipped artifact hash
gates are compatible with the post-376 state: existing files match this kit's
base and added files are absent. Do not apply this kit directly to a 353-only
deployment: finish and verify 376 first. The per-file hash gate remains
authoritative even when the deployment history is known.

## Step 0 - Choose The Deployment Profile

Read the customer's deployed `config.yml` and application configuration. Record
these choices before touching AWS:

| Option | Select `true` when | Apply sections |
|---|---|---|
| `tenant_query` | filtered tenant queries are required | Query GSIs, cursor key, API env |
| `tenant_stats` | `/tenants-stats` is required | Stats table, writer, schedule, IAM, route |
| `monitoring` | Prometheus/Grafana is deployed | host/Edge metrics SG and Prometheus config |
| `host_logs` | Firecracker host logs must reach Firehose | live host Fluent Bit and future S3 config |
| `edge_logs` | Edge logs must reach Firehose | live Edge Fluent Bit, S3, Edge LT |
| `user_hooks` | a reviewed S3 host-init hook is configured | host IAM and host LT only |

Core host/Lambda lifecycle fixes apply in every profile.

## Step 1 - Discover And Assess Impact

Start with the values the real customer client uses:

```bash
export REGION=<customer-region>
export API_BASE=<configured-control-plane-base-url>
export KEY=<customer-api-key>
aws sts get-caller-identity
curl -fsS -H "x-api-key: $KEY" "$API_BASE/tenants" | jq .
curl -fsS -H "x-api-key: $KEY" "$API_BASE/hosts" | jq .
```

Do not select an API by name. Resolve the URL to its REST API/custom-domain
mapping, then inspect the methods and integrations:

```bash
aws apigateway get-rest-apis --region "$REGION"
aws apigateway get-resources --rest-api-id "$API_ID" --region "$REGION"
aws apigateway get-integration --rest-api-id "$API_ID" --region "$REGION" \
  --resource-id "$TENANTS_RESOURCE_ID" --http-method GET
```

From the integration URI record the serving Lambda and qualifier. Determine
whether the qualifier is an alias, an immutable version, or absent (`$LATEST`).
Also list SQS event-source mappings; lifecycle dispatch commonly binds
`$LATEST`, independently of the API alias.

From the serving Lambda environment discover the assets bucket, tenants table,
hosts table, and feature flags:

```bash
aws lambda get-function-configuration --function-name "$API_FN" \
  --region "$REGION" > /tmp/api-config.before.json
jq '.Environment.Variables' /tmp/api-config.before.json
```

The API Lambda is not necessarily the only function running this package.
Discover the independent lifecycle consumer from the API's lifecycle queue.
Exactly one enabled event-source mapping must consume that queue, and its target
must be recorded separately from the API Lambda:

```bash
export LIFECYCLE_QUEUE_URL="$(jq -r \
  '.Environment.Variables.LIFECYCLE_QUEUE_URL // empty' \
  /tmp/api-config.before.json)"
test -n "$LIFECYCLE_QUEUE_URL"
export LIFECYCLE_QUEUE_ARN="$(aws sqs get-queue-attributes \
  --queue-url "$LIFECYCLE_QUEUE_URL" --attribute-names QueueArn \
  --region "$REGION" --query 'Attributes.QueueArn' --output text)"
aws lambda list-event-source-mappings \
  --event-source-arn "$LIFECYCLE_QUEUE_ARN" --region "$REGION" \
  >/tmp/lifecycle-esm.before.json
jq -e '.EventSourceMappings
  | length == 1 and .[0].State == "Enabled"' \
  /tmp/lifecycle-esm.before.json >/dev/null
export LIFECYCLE_ESM_UUID="$(jq -r \
  '.EventSourceMappings[0].UUID' /tmp/lifecycle-esm.before.json)"
export LIFECYCLE_FN_ARN="$(jq -r \
  '.EventSourceMappings[0].FunctionArn' /tmp/lifecycle-esm.before.json)"
export LIFECYCLE_FN="${LIFECYCLE_FN_ARN#*:function:}"
test -n "$LIFECYCLE_FN" && test "$LIFECYCLE_FN" != "$API_FN"
aws lambda get-function-configuration --function-name "$LIFECYCLE_FN" \
  --region "$REGION" >/tmp/lifecycle-config.before.json
```

Scan the hosts table and ignore rows whose `status` is `deleted`. Correlate that
active instance set with every ASG. Exactly one non-Edge ASG must have the same
instance set. Separately identify the Edge ASG and monitoring node from their
current instances, LT userdata, tags, and running services. Names are supporting
evidence, not identity.

Write the impact assessment:

- account, region, API URL/ID/stage, Lambda/qualifier;
- active host IDs and host ASG/LT version;
- Edge IDs and Edge ASG/LT version;
- selected options, Firehose stream, monitoring node;
- current failed services and expected post-fix signals.

Stop if any identity is ambiguous.

## Step 1.5 - Backup And Hash Gate

Use SHA-256. Compare each live file with `manifest.json`:

- live equals target: skip;
- live equals base: clean apply;
- neither: stop and review the diff with the customer.

Back up before writes:

```bash
# Lambda code/config and alias.
aws lambda get-function --function-name "$API_FN" --region "$REGION" \
  >/tmp/api-function.before.json
curl -fsSL "$(jq -r '.Code.Location' /tmp/api-function.before.json)" \
  -o /tmp/api.before.zip
aws lambda list-aliases --function-name "$API_FN" --region "$REGION" \
  > /tmp/api-aliases.before.json

# The lifecycle consumer is a separate deployment target, even when its zip
# initially matches the API package.
aws lambda get-function --function-name "$LIFECYCLE_FN" --region "$REGION" \
  >/tmp/lifecycle-function.before.json
curl -fsSL "$(jq -r '.Code.Location' /tmp/lifecycle-function.before.json)" \
  -o /tmp/lifecycle.before.zip

# S3 objects: record VersionId, or copy each object to a unique backup prefix.
aws s3api head-object --bucket "$BUCKET" \
  --key deployment/scripts/host-agent.py --region "$REGION"

# LT/ASG and API stage.
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names "$HOST_ASG" --region "$REGION" \
  > /tmp/host-asg.before.json
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names "$EDGE_ASG" --region "$REGION" \
  > /tmp/edge-asg.before.json
aws apigateway get-stage --rest-api-id "$API_ID" --stage-name "$STAGE" \
  --region "$REGION" > /tmp/api-stage.before.json
```

Unpack both `/tmp/api.before.zip` and `/tmp/lifecycle.before.zip`. Apply every
`C-lambda` hash gate independently to both packages: an existing artifact must
equal its manifest base or patch hash, and a manifest entry with an empty base
hash must be absent or already equal the patch hash. Stop if either package is
divergent. Updating only the API Lambda leaves queued delete operations running
old route-cleanup code and is not a valid deployment.

## Step 2 - Hot-Fix Running Hosts

Apply these full-file artifacts to every active Firecracker host:

| Artifact | Live destination | Future S3 key |
|---|---|---|
| `host-scripts/host-agent.py.patched` | `/opt/openclaw/host-agent.py` | `deployment/scripts/host-agent.py` |
| `host-scripts/route_ops.py.patched` | `/opt/openclaw/route_ops.py` | `deployment/scripts/route_ops.py` |
| `host-scripts/launch-vm.sh.patched` | `/home/ubuntu/launch-vm.sh` | `deployment/scripts/launch-vm.sh` |
| `host-scripts/stop-vm.sh.patched` | `/home/ubuntu/stop-vm.sh` | `deployment/scripts/stop-vm.sh` |
| `host-scripts/migrate-vm.sh.patched` | `/home/ubuntu/migrate-vm.sh` | `deployment/scripts/migrate-vm.sh` |

The five files form one host-side change. Install them transactionally on one
host at a time while lifecycle create/delete/migrate operations are paused for
that host. Do not install file-by-file. A validation or service failure must
restore all five files before the maintenance window is released.

Upload all artifacts to a unique temporary prefix and obtain target hashes from
the manifest:

```bash
export RUN_ID="monitor-patch-$(date -u +%Y%m%dT%H%M%SZ)"
export TEMP_PREFIX="patch-staging/$RUN_ID/hosts"
aws s3 cp "$PATCH_DIR/host-scripts/host-agent.py.patched" \
  "s3://$BUCKET/$TEMP_PREFIX/host-agent.py" --region "$REGION"
aws s3 cp "$PATCH_DIR/host-scripts/route_ops.py.patched" \
  "s3://$BUCKET/$TEMP_PREFIX/route_ops.py" --region "$REGION"
aws s3 cp "$PATCH_DIR/host-scripts/launch-vm.sh.patched" \
  "s3://$BUCKET/$TEMP_PREFIX/launch-vm.sh" --region "$REGION"
aws s3 cp "$PATCH_DIR/host-scripts/stop-vm.sh.patched" \
  "s3://$BUCKET/$TEMP_PREFIX/stop-vm.sh" --region "$REGION"
aws s3 cp "$PATCH_DIR/host-scripts/migrate-vm.sh.patched" \
  "s3://$BUCKET/$TEMP_PREFIX/migrate-vm.sh" --region "$REGION"

export HOST_AGENT_SHA="$(jq -r '.paths["deploy/userdata/host-agent.py"].patch_sha256' manifest.json)"
export ROUTE_OPS_SHA="$(jq -r '.paths["deploy/userdata/route_ops.py"].patch_sha256' manifest.json)"
export LAUNCH_SHA="$(jq -r '.paths["deploy/userdata/launch-vm.sh"].patch_sha256' manifest.json)"
export STOP_SHA="$(jq -r '.paths["deploy/userdata/stop-vm.sh"].patch_sha256' manifest.json)"
export MIGRATE_SHA="$(jq -r '.paths["deploy/userdata/migrate-vm.sh"].patch_sha256' manifest.json)"
```

Run this transaction once for each active host. Wait for success and verify the
host before continuing to the next host:

```bash
REMOTE_SCRIPT=$(cat <<'REMOTE'
set -Eeuo pipefail
REGION=$1
BUCKET=$2
PREFIX=$3
shift 3
NAMES=(host-agent.py route_ops.py launch-vm.sh stop-vm.sh migrate-vm.sh)
DESTS=(/opt/openclaw/host-agent.py /opt/openclaw/route_ops.py \
       /home/ubuntu/launch-vm.sh /home/ubuntu/stop-vm.sh \
       /home/ubuntu/migrate-vm.sh)
SHAS=("$@")
STAGE=$(mktemp -d /tmp/monitor-patch.XXXXXX)
BACKUP="/var/backups/openclaw-monitor-patch-$(date -u +%Y%m%dT%H%M%SZ)"
INSTALLED=0
rollback() {
  rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$INSTALLED" -eq 1 ]; then
    for i in "${!DESTS[@]}"; do
      cp -a -- "$BACKUP/${NAMES[$i]}" "${DESTS[$i]}" || true
    done
    systemctl restart host-agent || true
  fi
  rm -rf -- "$STAGE"
  exit "$rc"
}
trap rollback EXIT

for i in "${!NAMES[@]}"; do
  aws s3 cp "s3://$BUCKET/$PREFIX/${NAMES[$i]}" "$STAGE/${NAMES[$i]}" \
    --region "$REGION"
  printf '%s  %s\n' "${SHAS[$i]}" "$STAGE/${NAMES[$i]}" | sha256sum -c -
done
python3 -m py_compile "$STAGE/host-agent.py" "$STAGE/route_ops.py"
bash -n "$STAGE/launch-vm.sh" "$STAGE/stop-vm.sh" "$STAGE/migrate-vm.sh"

install -d -m 0700 "$BACKUP"
for i in "${!DESTS[@]}"; do
  cp -a -- "${DESTS[$i]}" "$BACKUP/${NAMES[$i]}"
done
INSTALLED=1
install -o root -g root -m 0644 "$STAGE/host-agent.py" /opt/openclaw/host-agent.py
install -o root -g root -m 0644 "$STAGE/route_ops.py" /opt/openclaw/route_ops.py
install -o root -g root -m 0755 "$STAGE/launch-vm.sh" /home/ubuntu/launch-vm.sh
install -o root -g root -m 0755 "$STAGE/stop-vm.sh" /home/ubuntu/stop-vm.sh
install -o root -g root -m 0755 "$STAGE/migrate-vm.sh" /home/ubuntu/migrate-vm.sh
systemctl restart host-agent
systemctl is-active --quiet host-agent
curl -fsS http://127.0.0.1:8899/metrics >/dev/null
for i in "${!DESTS[@]}"; do
  printf '%s  %s\n' "${SHAS[$i]}" "${DESTS[$i]}" | sha256sum -c -
done
INSTALLED=0
trap - EXIT
rm -rf -- "$STAGE"
echo "PASS host transaction backup=$BACKUP"
REMOTE
)

CMD=$(jq -nr \
  --arg script "$REMOTE_SCRIPT" --arg region "$REGION" --arg bucket "$BUCKET" \
  --arg prefix "$TEMP_PREFIX" --arg h1 "$HOST_AGENT_SHA" --arg h2 "$ROUTE_OPS_SHA" \
  --arg h3 "$LAUNCH_SHA" --arg h4 "$STOP_SHA" --arg h5 "$MIGRATE_SHA" \
  '["bash -s -- " + ([$region,$bucket,$prefix,$h1,$h2,$h3,$h4,$h5] | map(@sh) | join(" ")) +
    " <<\u0027REMOTE\u0027", $script, "REMOTE"] | join("\n")')
PARAMS=$(jq -nc --arg command "$CMD" \
  '{commands:[$command],executionTimeout:["300"]}')
COMMAND_ID=$(aws ssm send-command --instance-ids "$HOST_ID" --region "$REGION" \
  --document-name AWS-RunShellScript --parameters "$PARAMS" \
  --query 'Command.CommandId' --output text)
aws ssm wait command-executed --command-id "$COMMAND_ID" \
  --instance-id "$HOST_ID" --region "$REGION"
aws ssm get-command-invocation --command-id "$COMMAND_ID" \
  --instance-id "$HOST_ID" --region "$REGION" | jq .
```

After every live host passes, promote future-host S3 files as one recoverable
operation. This creates independent backup objects as well as recording S3
VersionIds, so rollback also works when bucket versioning is disabled:

```bash
set -Eeuo pipefail
CANON_NAMES=(host-agent.py route_ops.py launch-vm.sh stop-vm.sh migrate-vm.sh)
CANON_KEYS=(deployment/scripts/host-agent.py deployment/scripts/route_ops.py \
  deployment/scripts/launch-vm.sh deployment/scripts/stop-vm.sh \
  deployment/scripts/migrate-vm.sh)
CANON_SHAS=("$HOST_AGENT_SHA" "$ROUTE_OPS_SHA" "$LAUNCH_SHA" "$STOP_SHA" "$MIGRATE_SHA")
S3_STATE="/tmp/$RUN_ID-s3-state.jsonl"
: >"$S3_STATE"
for i in "${!CANON_KEYS[@]}"; do
  key=${CANON_KEYS[$i]}
  version=$(aws s3api head-object --bucket "$BUCKET" --key "$key" \
    --region "$REGION" --query 'VersionId || `null`' --output text)
  backup_key="patch-backups/$RUN_ID/$key"
  aws s3 cp "s3://$BUCKET/$key" "s3://$BUCKET/$backup_key" --region "$REGION"
  jq -nc --arg key "$key" --arg backup "$backup_key" --arg version "$version" \
    '{key:$key,backup_key:$backup,version_id:$version}' >>"$S3_STATE"
done

restore_s3() {
  while IFS= read -r row; do
    key=$(jq -r .key <<<"$row")
    backup=$(jq -r .backup_key <<<"$row")
    aws s3 cp "s3://$BUCKET/$backup" "s3://$BUCKET/$key" --region "$REGION"
  done <"$S3_STATE"
}
trap restore_s3 ERR
for i in "${!CANON_KEYS[@]}"; do
  aws s3 cp "s3://$BUCKET/$TEMP_PREFIX/${CANON_NAMES[$i]}" \
    "s3://$BUCKET/${CANON_KEYS[$i]}" --region "$REGION"
  actual=$(aws s3 cp "s3://$BUCKET/${CANON_KEYS[$i]}" - --region "$REGION" \
    | sha256sum | awk '{print $1}')
  test "$actual" = "${CANON_SHAS[$i]}"
done
trap - ERR
```

Retain the S3 state file, backup prefix, VersionIds, and per-host backup paths
until post-deployment verification is complete.

## Step 3 - API And Lifecycle Lambda Overlay

Do not replace the customer's native dependencies. Build separate overlays from
the two live packages. Each function retains its own dependencies and package
layout; only the same patch source files are overlaid:

```bash
API_OVERLAY_FILES=(
  handler.py
  core/auth.py
  core/clients.py
  core/pagination.py
  core/ssm_dispatch.py
  services/fleet_service.py
  services/host_service.py
  services/registry_service.py
  services/tenant_query_service.py
  services/tenant_service.py
  services/tenant_stats_service.py
)
build_api_overlay() {
  source_zip=$1
  work_dir=$2
  output_zip=$3
  rm -rf -- "$work_dir"
  mkdir -p "$work_dir"
  unzip -oq "$source_zip" -d "$work_dir"
  for rel in "${API_OVERLAY_FILES[@]}"; do
    mkdir -p "$work_dir/$(dirname "$rel")"
    install -m 0644 "$PATCH_DIR/lambda/api/$rel" "$work_dir/$rel"
  done
  python3 -m compileall -q "$work_dir"
  rm -f "$output_zip"
  (cd "$work_dir" && zip -qr "$output_zip" .)
}
build_api_overlay /tmp/api.before.zip \
  /tmp/api-overlay/live /tmp/api.patched.zip
build_api_overlay /tmp/lifecycle.before.zip \
  /tmp/lifecycle-overlay/live /tmp/lifecycle.patched.zip
```

Pause the lifecycle event-source mapping before either code write. Stop normal
create/delete/migrate operations for this maintenance window. If either update
or verification fails, restore both saved zips before returning the mapping to
its captured state:

```bash
export LIFECYCLE_ESM_WAS_ENABLED="$(jq -r \
  '.EventSourceMappings[0].State == "Enabled"' /tmp/lifecycle-esm.before.json)"
aws lambda update-event-source-mapping --uuid "$LIFECYCLE_ESM_UUID" \
  --no-enabled --region "$REGION" >/tmp/lifecycle-esm.disabled.json
for attempt in $(seq 1 30); do
  state=$(aws lambda get-event-source-mapping --uuid "$LIFECYCLE_ESM_UUID" \
    --region "$REGION" --query State --output text)
  [ "$state" = Disabled ] && break
  sleep 2
done
test "$state" = Disabled

LIFECYCLE_REV=$(aws lambda get-function-configuration \
  --function-name "$LIFECYCLE_FN" --region "$REGION" \
  --query RevisionId --output text)
aws lambda update-function-code --function-name "$LIFECYCLE_FN" \
  --region "$REGION" --revision-id "$LIFECYCLE_REV" \
  --zip-file fileb:///tmp/lifecycle.patched.zip >/dev/null
aws lambda wait function-updated --function-name "$LIFECYCLE_FN" \
  --region "$REGION"

API_REV=$(aws lambda get-function-configuration --function-name "$API_FN" \
  --region "$REGION" --query RevisionId --output text)
aws lambda update-function-code --function-name "$API_FN" --region "$REGION" \
  --revision-id "$API_REV" --zip-file fileb:///tmp/api.patched.zip >/dev/null
aws lambda wait function-updated --function-name "$API_FN" --region "$REGION"

python3 - /tmp/lifecycle.patched.zip "$LIFECYCLE_FN" \
  /tmp/api.patched.zip "$API_FN" "$REGION" <<'PY'
import base64
import hashlib
import subprocess
import sys

region = sys.argv[-1]
for package, function_name in zip(sys.argv[1:-1:2], sys.argv[2:-1:2]):
    with open(package, "rb") as stream:
        local = base64.b64encode(hashlib.sha256(stream.read()).digest()).decode()
    remote = subprocess.check_output(
        [
            "aws", "lambda", "get-function-configuration",
            "--function-name", function_name, "--region", region,
            "--query", "CodeSha256", "--output", "text",
        ],
        text=True,
    ).strip()
    assert local == remote, (function_name, local, remote)
PY

if [ "$LIFECYCLE_ESM_WAS_ENABLED" = true ]; then
  aws lambda update-event-source-mapping --uuid "$LIFECYCLE_ESM_UUID" \
    --enabled --region "$REGION" >/tmp/lifecycle-esm.enabled.json
  for attempt in $(seq 1 30); do
    state=$(aws lambda get-event-source-mapping --uuid "$LIFECYCLE_ESM_UUID" \
      --region "$REGION" --query State --output text)
    [ "$state" = Enabled ] && break
    sleep 2
  done
  test "$state" = Enabled
fi
```

Probe API `$LATEST` before changing the serving alias. Build the payload with
`jq`; it must include `httpMethod`, `resource`, and `path`. Success means
invocation metadata has no `FunctionError` (a 404 response body for the
synthetic path is acceptable). The lifecycle consumer is verified by the code
checksum above and by the real queued delete in Step 7; do not send a fabricated
SQS record that could mutate tenant state:

```bash
jq -nc '{httpMethod:"GET",resource:"/__monitor_patch_probe",
  path:"/__monitor_patch_probe",headers:{},queryStringParameters:null,
  requestContext:{identity:{}}}' >/tmp/api-probe.json
aws lambda invoke --function-name "$API_FN" --region "$REGION" \
  --qualifier '$LATEST' --payload fileb:///tmp/api-probe.json \
  /tmp/api-probe.out.json >/tmp/api-probe.meta.json
test "$(jq -r '.FunctionError // empty' /tmp/api-probe.meta.json)" = ""
```

If `tenant_query` or `tenant_stats` is selected, do not publish or move the
serving alias yet; Step 4 must first update `$LATEST` configuration. Otherwise,
publish now and record the returned version. If API Gateway invokes an alias,
capture its current `FunctionVersion` and update that same alias. If it invokes
`$LATEST`, do not invent an alias. If it invokes an immutable numeric version,
stop and have the customer approve an integration change. Never silently point
a different API at the function.

Repeat the overlay for `lambda/health_check/handler.py`. Use Lambda `DryRun` for
health-check verification because a real invocation can restart hosts.

## Step 4 - Optional Resources And API

The commands in this step are self-contained. They do not call `lib/*`. Before
running them, derive these values from the live API Lambda configuration saved
in Step 1:

```bash
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export PARTITION="$(aws sts get-caller-identity --query Arn --output text | cut -d: -f2)"
export TENANTS_TABLE="$(jq -r '.Environment.Variables.TENANTS_TABLE' /tmp/api-config.before.json)"
export ASSETS_BUCKET="$(jq -r '.Environment.Variables.ASSETS_BUCKET' /tmp/api-config.before.json)"
export ROOTFS_PREFIX="$(jq -r '.Environment.Variables.ROOTFS_PREFIX' /tmp/api-config.before.json)"
export API_ROLE_ARN="$(jq -r '.Role' /tmp/api-config.before.json)"
export API_ROLE_NAME="${API_ROLE_ARN##*/}"
test -n "$TENANTS_TABLE" && test "$TENANTS_TABLE" != null
test -n "$ASSETS_BUCKET" && test "$ASSETS_BUCKET" != null
test -n "$ROOTFS_PREFIX" && test "$ROOTFS_PREFIX" != null
```

### Tenant Query

Only when `tenant_query=true`, create or adopt the cursor secret. Existing
secret values must be retained so already-issued cursors remain decryptable:

```bash
export CURSOR_SECRET_NAME="openclaw/pagination-cursor"
if aws secretsmanager get-secret-value --secret-id "$CURSOR_SECRET_NAME" \
  --region "$REGION" >/tmp/pagination-secret.json 2>/dev/null; then
  export PAGINATION_AES_KEY="$(jq -r '.SecretString | fromjson | .key' \
    /tmp/pagination-secret.json)"
else
  export PAGINATION_AES_KEY="$(python3 -c \
    'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))')"
  aws secretsmanager create-secret --name "$CURSOR_SECRET_NAME" \
    --secret-string "$(jq -nc --arg key "$PAGINATION_AES_KEY" \
      '{purpose:"pagination-aes-gcm",key:$key}')" --region "$REGION" \
    >/tmp/pagination-secret.created.json
fi
python3 - "$PAGINATION_AES_KEY" <<'PY'
import base64, sys
value = sys.argv[1]
raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
assert len(raw) == 32, "pagination key must decode to exactly 32 bytes"
PY

jq --arg key "$PAGINATION_AES_KEY" \
  '.Environment.Variables + {PAGINATION_AES_KEY:$key}
   | {Variables:.}' /tmp/api-config.before.json >/tmp/api-env.query-key.json
REV=$(aws lambda get-function-configuration --function-name "$API_FN" \
  --region "$REGION" --query RevisionId --output text)
aws lambda update-function-configuration --function-name "$API_FN" \
  --region "$REGION" --revision-id "$REV" \
  --environment file:///tmp/api-env.query-key.json
aws lambda wait function-updated --function-name "$API_FN" --region "$REGION"
```

Create or validate one GSI at a time. This inline shell function refuses a
mismatched existing index and waits for both the table and index to become
`ACTIVE`:

```bash
ensure_gsi() {
  index=$1
  attribute=$2
  table_json=$(aws dynamodb describe-table --table-name "$TENANTS_TABLE" \
    --region "$REGION")
  if jq -e --arg index "$index" \
    '.Table.GlobalSecondaryIndexes[]? | select(.IndexName == $index)' \
    >/dev/null <<<"$table_json"; then
    jq -e --arg index "$index" --arg attribute "$attribute" '
      [.Table.GlobalSecondaryIndexes[] | select(.IndexName == $index)] as $g
      | ($g | length) == 1
        and $g[0].KeySchema == [{AttributeName:$attribute,KeyType:"HASH"}]
        and $g[0].Projection.ProjectionType == "ALL"' \
      >/dev/null <<<"$table_json" || {
        echo "FATAL: existing $index does not match the required contract" >&2
        return 1
      }
  else
    attrs=$(jq -nc --arg attribute "$attribute" \
      '[{AttributeName:$attribute,AttributeType:"S"}]')
    updates=$(jq -nc --arg index "$index" --arg attribute "$attribute" \
      '[{Create:{IndexName:$index,
        KeySchema:[{AttributeName:$attribute,KeyType:"HASH"}],
        Projection:{ProjectionType:"ALL"}}}]')
    aws dynamodb update-table --table-name "$TENANTS_TABLE" \
      --attribute-definitions "$attrs" \
      --global-secondary-index-updates "$updates" --region "$REGION" >/dev/null
  fi
  for attempt in $(seq 1 120); do
    state=$(aws dynamodb describe-table --table-name "$TENANTS_TABLE" \
      --region "$REGION")
    if jq -e --arg index "$index" '
      .Table.TableStatus == "ACTIVE"
      and ([.Table.GlobalSecondaryIndexes[]
        | select(.IndexName == $index and .IndexStatus == "ACTIVE")] | length) == 1' \
      >/dev/null <<<"$state"; then
      echo "PASS: $index ACTIVE"
      return 0
    fi
    sleep 10
  done
  echo "FATAL: timeout waiting for $index" >&2
  return 1
}

ensure_gsi gsi_tenant_user tenant_user_id
ensure_gsi gsi_host host_id
ensure_gsi gsi_status status
```

Backfill only valid string `rootfs_version` values, preserving conditional
write safety, then create the fourth GSI:

```bash
aws dynamodb scan --table-name "$TENANTS_TABLE" --region "$REGION" \
  --projection-expression "id, rootfs_version, q_rootfs_version" \
  >/tmp/rootfs-backfill.json
while IFS= read -r row; do
  id=$(jq -r .id <<<"$row")
  version=$(jq -r .version <<<"$row")
  key=$(jq -nc --arg id "$id" '{id:{S:$id}}')
  values=$(jq -nc --arg version "$version" '{":v":{S:$version}}')
  aws dynamodb update-item --table-name "$TENANTS_TABLE" --region "$REGION" \
    --key "$key" --update-expression "SET q_rootfs_version = :v" \
    --condition-expression "rootfs_version = :v" \
    --expression-attribute-values "$values" >/dev/null
done < <(jq -c '
  .Items[]
  | select(.id.S and .rootfs_version.S)
  | select((.rootfs_version.S | utf8bytelength) <= 256)
  | select((.q_rootfs_version.S // "") != .rootfs_version.S)
  | {id:.id.S,version:.rootfs_version.S}' /tmp/rootfs-backfill.json)

ensure_gsi gsi_rootfs_version q_rootfs_version
```

Only after all four indexes are `ACTIVE`, merge the final query flag into the
current `$LATEST` environment. Re-read the environment here so the earlier
update cannot erase concurrent or stats changes:

```bash
aws lambda get-function-configuration --function-name "$API_FN" \
  --region "$REGION" >/tmp/api-config.query-current.json
jq --arg key "$PAGINATION_AES_KEY" \
  '.Environment.Variables
   + {PAGINATION_AES_KEY:$key,TENANT_QUERY_ENABLED:"true"}
   | {Variables:.}' /tmp/api-config.query-current.json >/tmp/api-env.query-final.json
REV=$(jq -r .RevisionId /tmp/api-config.query-current.json)
aws lambda update-function-configuration --function-name "$API_FN" \
  --region "$REGION" --revision-id "$REV" \
  --environment file:///tmp/api-env.query-final.json
aws lambda wait function-updated --function-name "$API_FN" --region "$REGION"
```

### Tenant Stats

Only when `tenant_stats=true`, create or validate the table and enable PITR:

```bash
export STATS_TABLE="openclaw-tenant-stats"
if ! aws dynamodb describe-table --table-name "$STATS_TABLE" \
  --region "$REGION" >/tmp/stats-table.json 2>/dev/null; then
  aws dynamodb create-table --table-name "$STATS_TABLE" \
    --attribute-definitions AttributeName=id,AttributeType=S \
    --key-schema AttributeName=id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST --region "$REGION" >/dev/null
  aws dynamodb wait table-exists --table-name "$STATS_TABLE" --region "$REGION"
fi
aws dynamodb describe-table --table-name "$STATS_TABLE" --region "$REGION" \
  >/tmp/stats-table.json
jq -e '.Table.TableStatus == "ACTIVE"
  and .Table.BillingModeSummary.BillingMode == "PAY_PER_REQUEST"
  and .Table.KeySchema == [{AttributeName:"id",KeyType:"HASH"}]' \
  /tmp/stats-table.json >/dev/null
aws dynamodb update-continuous-backups --table-name "$STATS_TABLE" \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
  --region "$REGION" >/dev/null
export TENANTS_TABLE_ARN="$(aws dynamodb describe-table \
  --table-name "$TENANTS_TABLE" --region "$REGION" --query Table.TableArn --output text)"
export STATS_TABLE_ARN="$(jq -r .Table.TableArn /tmp/stats-table.json)"
```

Create or validate the writer role, then install a least-privilege inline
policy. Capture any existing trust and inline policy first for rollback:

```bash
export STATS_ROLE_NAME="openclaw-tenant-stats-writer-role"
jq -nc '{Version:"2012-10-17",Statement:[{Effect:"Allow",
  Principal:{Service:"lambda.amazonaws.com"},Action:"sts:AssumeRole"}]}' \
  >/tmp/stats-trust.json
if ! aws iam get-role --role-name "$STATS_ROLE_NAME" \
  >/tmp/stats-role.before.json 2>/dev/null; then
  aws iam create-role --role-name "$STATS_ROLE_NAME" \
    --assume-role-policy-document file:///tmp/stats-trust.json \
    >/tmp/stats-role.created.json
else
  aws iam get-role --role-name "$STATS_ROLE_NAME" \
    | jq -e '.Role.AssumeRolePolicyDocument.Statement[]
      | select(.Effect == "Allow"
        and .Principal.Service == "lambda.amazonaws.com"
        and .Action == "sts:AssumeRole")' >/dev/null
fi
export STATS_ROLE_ARN="$(aws iam get-role --role-name "$STATS_ROLE_NAME" \
  --query Role.Arn --output text)"
jq -nc --arg tenants "$TENANTS_TABLE_ARN" --arg stats "$STATS_TABLE_ARN" \
  --arg partition "$PARTITION" --arg bucket "$ASSETS_BUCKET" \
  --arg prefix "$ROOTFS_PREFIX" '{
  Version:"2012-10-17",Statement:[
    {Effect:"Allow",Action:["logs:CreateLogGroup","logs:CreateLogStream",
      "logs:PutLogEvents"],Resource:"*"},
    {Effect:"Allow",Action:["dynamodb:Scan","dynamodb:DescribeTable"],
      Resource:$tenants},
    {Effect:"Allow",Action:["dynamodb:GetItem","dynamodb:PutItem",
      "dynamodb:DescribeTable"],Resource:$stats},
    {Effect:"Allow",Action:"s3:GetObject",
      Resource:("arn:" + $partition + ":s3:::" + $bucket + "/" + $prefix + "/*")}
  ]}' >/tmp/stats-writer-policy.json
aws iam put-role-policy --role-name "$STATS_ROLE_NAME" \
  --policy-name monitor-patch-tenant-stats-writer \
  --policy-document file:///tmp/stats-writer-policy.json
```

Package and create/update the writer. An existing non-arm64 function is a hard
stop because architecture cannot be changed with
`update-function-configuration`:

```bash
rm -rf /tmp/stats-writer-package
mkdir -p /tmp/stats-writer-package
install -m 0644 "$PATCH_DIR/lambda/tenant_stats/handler.py" \
  /tmp/stats-writer-package/handler.py
(cd /tmp/stats-writer-package && zip -q /tmp/stats-writer.zip handler.py)
export STATS_FN="openclaw-tenant-stats-writer"
STATS_ENV=$(jq -nc --arg tenants "$TENANTS_TABLE" --arg stats "$STATS_TABLE" \
  --arg bucket "$ASSETS_BUCKET" --arg prefix "$ROOTFS_PREFIX" \
  '{Variables:{TENANTS_TABLE:$tenants,TENANT_STATS_TABLE:$stats,
    ASSETS_BUCKET:$bucket,ROOTFS_PREFIX:$prefix,STATS_SCAN_SEGMENTS:"8"}}')
if aws lambda get-function-configuration --function-name "$STATS_FN" \
  --region "$REGION" >/tmp/stats-fn.before.json 2>/dev/null; then
  jq -e '.Architectures == ["arm64"]' /tmp/stats-fn.before.json >/dev/null
  REV=$(jq -r .RevisionId /tmp/stats-fn.before.json)
  aws lambda update-function-code --function-name "$STATS_FN" \
    --revision-id "$REV" --zip-file fileb:///tmp/stats-writer.zip \
    --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$STATS_FN" --region "$REGION"
  REV=$(aws lambda get-function-configuration --function-name "$STATS_FN" \
    --region "$REGION" --query RevisionId --output text)
  aws lambda update-function-configuration --function-name "$STATS_FN" \
    --revision-id "$REV" --role "$STATS_ROLE_ARN" --runtime python3.12 \
    --handler handler.lambda_handler --timeout 50 --memory-size 8192 \
    --environment "$STATS_ENV" --region "$REGION" >/dev/null
else
  aws lambda create-function --function-name "$STATS_FN" \
    --runtime python3.12 --architectures arm64 --role "$STATS_ROLE_ARN" \
    --handler handler.lambda_handler --timeout 50 --memory-size 8192 \
    --environment "$STATS_ENV" --zip-file fileb:///tmp/stats-writer.zip \
    --region "$REGION" >/dev/null
fi
aws lambda wait function-updated --function-name "$STATS_FN" --region "$REGION"
aws lambda put-function-concurrency --function-name "$STATS_FN" \
  --reserved-concurrent-executions 1 --region "$REGION" >/dev/null
```

Create the one-minute EventBridge schedule, exact Lambda permission, and target:

```bash
export STATS_RULE="openclaw-tenant-stats-schedule"
export STATS_FN_ARN="$(aws lambda get-function-configuration \
  --function-name "$STATS_FN" --region "$REGION" --query FunctionArn --output text)"
export STATS_RULE_ARN="$(aws events put-rule --name "$STATS_RULE" \
  --schedule-expression 'rate(1 minute)' --state ENABLED --region "$REGION" \
  --query RuleArn --output text)"
if ! aws lambda add-permission --function-name "$STATS_FN" \
  --statement-id monitor-patch-stats-schedule \
  --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn "$STATS_RULE_ARN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda get-policy --function-name "$STATS_FN" --region "$REGION" \
    --query Policy --output text >/tmp/stats-writer-resource-policy.json
  jq -e --arg arn "$STATS_RULE_ARN" '.Statement[]
    | select(.Sid == "monitor-patch-stats-schedule"
      and .Effect == "Allow"
      and .Principal.Service == "events.amazonaws.com"
      and .Action == "lambda:InvokeFunction"
      and ((.Condition.ArnLike["AWS:SourceArn"] // .Condition.ArnEquals["AWS:SourceArn"]) == $arn))' \
    /tmp/stats-writer-resource-policy.json >/dev/null
fi
TARGET_RESULT=$(aws events put-targets --rule "$STATS_RULE" --region "$REGION" \
  --targets "$(jq -nc --arg arn "$STATS_FN_ARN" \
    '[{Id:"tenant-stats-writer",Arn:$arn}]')")
test "$(jq -r .FailedEntryCount <<<"$TARGET_RESULT")" = 0
aws lambda invoke --function-name "$STATS_FN" --region "$REGION" \
  /tmp/stats-writer-probe.json >/tmp/stats-writer-probe.meta.json
test "$(jq -r '.FunctionError // empty' /tmp/stats-writer-probe.meta.json)" = ""
aws dynamodb get-item --table-name "$STATS_TABLE" \
  --key '{"id":{"S":"current"}}' --consistent-read --region "$REGION" \
  | jq -e '.Item.refreshed_at.S'
```

Grant the serving API role read access and merge the stats table into the
current API environment:

```bash
jq -nc --arg stats "$STATS_TABLE_ARN" '{Version:"2012-10-17",
  Statement:[{Effect:"Allow",Action:["dynamodb:GetItem","dynamodb:DescribeTable"],
    Resource:$stats}]}' >/tmp/api-stats-read-policy.json
aws iam put-role-policy --role-name "$API_ROLE_NAME" \
  --policy-name monitor-patch-tenant-stats-read \
  --policy-document file:///tmp/api-stats-read-policy.json
aws lambda get-function-configuration --function-name "$API_FN" \
  --region "$REGION" >/tmp/api-config.stats-current.json
jq --arg table "$STATS_TABLE" \
  '.Environment.Variables + {TENANT_STATS_TABLE:$table}
   | {Variables:.}' /tmp/api-config.stats-current.json >/tmp/api-env.stats-final.json
REV=$(jq -r .RevisionId /tmp/api-config.stats-current.json)
aws lambda update-function-configuration --function-name "$API_FN" \
  --region "$REGION" --revision-id "$REV" \
  --environment file:///tmp/api-env.stats-final.json
aws lambda wait function-updated --function-name "$API_FN" --region "$REGION"
```

After all selected query/stats configuration is present on `$LATEST`, repeat
the Step 3 probe. Then publish exactly one final version and move only the alias
already used by API Gateway:

```bash
export NEW_API_VERSION="$(aws lambda publish-version --function-name "$API_FN" \
  --region "$REGION" --query Version --output text)"
if [ -n "${API_ALIAS:-}" ]; then
  aws lambda get-alias --function-name "$API_FN" --name "$API_ALIAS" \
    --region "$REGION" >/tmp/api-alias.before-switch.json
  aws lambda update-alias --function-name "$API_FN" --name "$API_ALIAS" \
    --function-version "$NEW_API_VERSION" \
    --revision-id "$(jq -r .RevisionId /tmp/api-alias.before-switch.json)" \
    --region "$REGION" >/tmp/api-alias.after-switch.json
fi
```

If the integration uses `$LATEST`, leave `API_ALIAS` unset and do not change an
alias. Rollback restores the old alias `FunctionVersion` and the previous
Lambda environment captured in Step 1.

### `/tenants-stats`, API Key, And Authorizer

Call the route with the real customer auth first:

- `200`: already complete, skip;
- `503`: route exists; finish stats resources;
- `403` with `Missing Authentication Token`: create the route;
- any other `403`: inspect API-key requirement, authorizer, and resource policy.

For a missing route, copy the exact method auth, API-key requirement, authorizer
ID/scopes, Lambda AWS_PROXY URI/qualifier, credentials, and integration HTTP
method from `GET /tenants`. Stop on a partially present or mismatched route.

```bash
export ROOT_RID="$(aws apigateway get-resources --rest-api-id "$API_ID" \
  --region "$REGION" --query 'items[?path==`/`].id | [0]' --output text)"
export TENANTS_RID="$(aws apigateway get-resources --rest-api-id "$API_ID" \
  --region "$REGION" --query 'items[?path==`/tenants`].id | [0]' --output text)"
test -n "$ROOT_RID" && test "$ROOT_RID" != None
test -n "$TENANTS_RID" && test "$TENANTS_RID" != None
SOURCE_METHOD=$(aws apigateway get-method --rest-api-id "$API_ID" \
  --resource-id "$TENANTS_RID" --http-method GET --region "$REGION")
SOURCE_INTEGRATION=$(aws apigateway get-integration --rest-api-id "$API_ID" \
  --resource-id "$TENANTS_RID" --http-method GET --region "$REGION")
test "$(jq -r .type <<<"$SOURCE_INTEGRATION")" = AWS_PROXY
export PREVIOUS_DEPLOYMENT="$(aws apigateway get-stage --rest-api-id "$API_ID" \
  --stage-name "$STAGE" --region "$REGION" --query deploymentId --output text)"

EXISTING_STATS_RID="$(aws apigateway get-resources --rest-api-id "$API_ID" \
  --region "$REGION" --query 'items[?path==`/tenants-stats`].id | [0]' --output text)"
test "$EXISTING_STATS_RID" = None || {
  echo "FATAL: /tenants-stats already exists; validate/adopt it instead of overwriting" >&2
  exit 1
}
export STATS_RID="$(aws apigateway create-resource --rest-api-id "$API_ID" \
  --parent-id "$ROOT_RID" --path-part tenants-stats --region "$REGION" \
  --query id --output text)"

METHOD_INPUT=$(jq -nc --arg api "$API_ID" --arg rid "$STATS_RID" \
  --argjson source "$SOURCE_METHOD" '{
    restApiId:$api,resourceId:$rid,httpMethod:"GET",
    authorizationType:$source.authorizationType,
    apiKeyRequired:($source.apiKeyRequired // false)}
    + (if $source.authorizerId then {authorizerId:$source.authorizerId} else {} end)
    + (if (($source.authorizationScopes // []) | length) > 0
       then {authorizationScopes:$source.authorizationScopes} else {} end)')
aws apigateway put-method --cli-input-json "$METHOD_INPUT" --region "$REGION"

INTEGRATION_INPUT=$(jq -nc --arg api "$API_ID" --arg rid "$STATS_RID" \
  --argjson source "$SOURCE_INTEGRATION" '{
    restApiId:$api,resourceId:$rid,httpMethod:"GET",type:$source.type,
    integrationHttpMethod:$source.httpMethod,uri:$source.uri,
    passthroughBehavior:($source.passthroughBehavior // "WHEN_NO_MATCH"),
    timeoutInMillis:($source.timeoutInMillis // 29000)}
    + (if $source.credentials then {credentials:$source.credentials} else {} end)
    + (if $source.contentHandling then {contentHandling:$source.contentHandling} else {} end)')
aws apigateway put-integration --cli-input-json "$INTEGRATION_INPUT" \
  --region "$REGION"

aws apigateway put-method --rest-api-id "$API_ID" --resource-id "$STATS_RID" \
  --http-method OPTIONS --authorization-type NONE --no-api-key-required \
  --region "$REGION"
aws apigateway put-integration --rest-api-id "$API_ID" \
  --resource-id "$STATS_RID" --http-method OPTIONS --type MOCK \
  --request-templates '{"application/json":"{ statusCode: 200 }"}' \
  --passthrough-behavior WHEN_NO_MATCH --region "$REGION"
aws apigateway put-method-response --rest-api-id "$API_ID" \
  --resource-id "$STATS_RID" --http-method OPTIONS --status-code 204 \
  --response-parameters 'method.response.header.Access-Control-Allow-Headers=true,method.response.header.Access-Control-Allow-Methods=true,method.response.header.Access-Control-Allow-Origin=true' \
  --region "$REGION"
aws apigateway put-integration-response --rest-api-id "$API_ID" \
  --resource-id "$STATS_RID" --http-method OPTIONS --status-code 204 \
  --response-parameters "method.response.header.Access-Control-Allow-Headers='Content-Type,x-api-key,Authorization',method.response.header.Access-Control-Allow-Methods='OPTIONS,GET',method.response.header.Access-Control-Allow-Origin='*'" \
  --region "$REGION"

PERMISSION_ARGS=(--function-name "$API_FN")
if [ -n "${API_ALIAS:-}" ]; then
  PERMISSION_ARGS+=(--qualifier "$API_ALIAS")
fi
export STATS_ROUTE_SOURCE_ARN="arn:$PARTITION:execute-api:$REGION:$ACCOUNT_ID:$API_ID/*/GET/tenants-stats"
if ! aws lambda add-permission "${PERMISSION_ARGS[@]}" \
  --statement-id monitor-patch-tenants-stats \
  --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
  --source-arn "$STATS_ROUTE_SOURCE_ARN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda get-policy "${PERMISSION_ARGS[@]}" --region "$REGION" \
    --query Policy --output text >/tmp/api-resource-policy.json
  jq -e --arg arn "$STATS_ROUTE_SOURCE_ARN" '.Statement[]
    | select(.Sid == "monitor-patch-tenants-stats"
      and .Effect == "Allow"
      and .Principal.Service == "apigateway.amazonaws.com"
      and .Action == "lambda:InvokeFunction"
      and ((.Condition.ArnLike["AWS:SourceArn"]
        // .Condition.ArnEquals["AWS:SourceArn"]) == $arn))' \
    /tmp/api-resource-policy.json >/dev/null
fi
```

Poll until both methods and integrations can be read back. Compare the GET
method auth fields and integration URI to the saved source before deployment:

```bash
for attempt in $(seq 1 30); do
  aws apigateway get-method --rest-api-id "$API_ID" --resource-id "$STATS_RID" \
    --http-method GET --region "$REGION" >/tmp/stats-method.json 2>/dev/null &&
  aws apigateway get-integration --rest-api-id "$API_ID" \
    --resource-id "$STATS_RID" --http-method GET --region "$REGION" \
    >/tmp/stats-integration.json 2>/dev/null && break
  sleep 2
done
jq -e --argjson source "$SOURCE_METHOD" '
  {authorizationType,apiKeyRequired,authorizerId,authorizationScopes}
  == ($source | {authorizationType,apiKeyRequired,authorizerId,authorizationScopes})' \
  /tmp/stats-method.json >/dev/null
jq -e --argjson source "$SOURCE_INTEGRATION" '
  {type,httpMethod,uri,credentials}
  == ($source | {type,httpMethod,uri,credentials})' \
  /tmp/stats-integration.json >/dev/null
test "$(aws apigateway get-stage --rest-api-id "$API_ID" --stage-name "$STAGE" \
  --region "$REGION" --query deploymentId --output text)" = "$PREVIOUS_DEPLOYMENT"
export NEW_DEPLOYMENT="$(aws apigateway create-deployment \
  --rest-api-id "$API_ID" --description "monitor-patch tenants-stats" \
  --region "$REGION" --query id --output text)"
aws apigateway update-stage --rest-api-id "$API_ID" --stage-name "$STAGE" \
  --patch-operations "op=replace,path=/deploymentId,value=$NEW_DEPLOYMENT" \
  --region "$REGION" >/dev/null
```

Probe with the customer's real authentication. A non-authenticated `403` can be
expected; an authenticated `403` must be inspected. In particular,
`Missing Authentication Token` means API Gateway has not exposed the route, not
that the Lambda authorizer rejected the caller:

```bash
probe_stats_route() {
  for attempt in $(seq 1 12); do
    code=$(curl -sS -o /tmp/tenants-stats.body -w '%{http_code}' \
      -H "x-api-key: $KEY" "$API_BASE/tenants-stats")
    if [ "$code" = 200 ]; then
      jq -e '.business.total | type == "number"' /tmp/tenants-stats.body >/dev/null
      return 0
    fi
    if [ "$code" = 403 ] &&
      ! jq -e '.message == "Missing Authentication Token"' \
        /tmp/tenants-stats.body >/dev/null 2>&1; then
      echo "FATAL: authenticated request rejected by API key/authorizer/policy" >&2
      cat /tmp/tenants-stats.body >&2
      return 1
    fi
    sleep 5
  done
  return 2
}

if probe_stats_route; then
  :
else
  rc=$?
  test "$rc" = 2 || exit "$rc"
  test "$(aws apigateway get-stage --rest-api-id "$API_ID" \
    --stage-name "$STAGE" --region "$REGION" \
    --query deploymentId --output text)" = "$NEW_DEPLOYMENT"
  aws apigateway get-method --rest-api-id "$API_ID" \
    --resource-id "$STATS_RID" --http-method GET --region "$REGION" >/dev/null
  aws apigateway get-integration --rest-api-id "$API_ID" \
    --resource-id "$STATS_RID" --http-method GET --region "$REGION" >/dev/null
  export SECOND_DEPLOYMENT="$(aws apigateway create-deployment \
    --rest-api-id "$API_ID" \
    --description "monitor-patch tenants-stats propagation retry" \
    --region "$REGION" --query id --output text)"
  aws apigateway update-stage --rest-api-id "$API_ID" --stage-name "$STAGE" \
    --patch-operations "op=replace,path=/deploymentId,value=$SECOND_DEPLOYMENT" \
    --region "$REGION" >/dev/null
  probe_stats_route
fi
```

Rollback must first restore `PREVIOUS_DEPLOYMENT`, then delete only
`STATS_RID`. Never restore the stage if its current deployment is neither
`NEW_DEPLOYMENT` nor `SECOND_DEPLOYMENT`; that means another deployment has
occurred and requires review.

### Monitoring Network

Only when `monitoring=true`, describe the rules first and request explicit
approval for:

- VPC CIDR -> host SG TCP 8899;
- VPC CIDR -> Edge SG TCP 9145.

Do not add duplicate or public `0.0.0.0/0` metrics rules. Record only rules
created by this patch so rollback cannot remove pre-existing access.

## Step 5 - Host And Edge Fluent Bit

When `host_logs=true`, hash-gate the unrendered canonical object
`deployment/observability/fluent-bit/host/fluent-bit.conf` against the manifest,
then promote `host-scripts/edge/fluent-bit/host/fluent-bit.conf` there for future
hosts. The live `/etc/fluent-bit/fluent-bit.conf` is a rendered file and must
not be compared directly with the source artifact hash. On each active host,
record its current non-empty region and both Firehose streams, back up the whole
`/etc/fluent-bit` directory, render the source template with those three values,
run Fluent Bit `--dry-run`, restart the service, and confirm it is active.
Restore the directory backup on any failure.

When either `edge_logs=true` or `monitoring=true`, apply
`host-scripts/edge/nginx.conf` to all running Edge instances through a temporary
S3 key and SSM. When `edge_logs=true`, also apply:

- `host-scripts/edge/install-fluent-bit.sh`;
- `host-scripts/edge/fluent-bit/edge/*`.

Install the source files under `/opt/openclaw-edge`, preserving a backup of that
directory and `/etc/fluent-bit`. Discover the live Redis endpoint from the
current environment and use a non-empty customer region and Firehose stream.
Re-run the customer entry point with explicit inputs:

```bash
test -x /usr/local/openresty/nginx/sbin/nginx
test -f /opt/openclaw-edge/install-edge.sh
test -f /opt/openclaw-edge/nginx.conf
test -n "$ENGINE_REDIS_ENDPOINT"
if [ "$EDGE_LOGS_ENABLED" = true ]; then
  test -n "$FIREHOSE_DELIVERY_STREAM"
fi
ENGINE_REDIS_ENDPOINT="$ENGINE_REDIS_ENDPOINT" \
EDGE_LISTEN_PORT=8080 \
LOGGING_ENABLED="$EDGE_LOGS_ENABLED" \
ASSETS_BUCKET="$BUCKET" \
AWS_REGION="$REGION" \
FIREHOSE_DELIVERY_STREAM="$FIREHOSE_DELIVERY_STREAM" \
  bash /opt/openclaw-edge/install-edge.sh
/usr/local/openresty/nginx/sbin/nginx -t \
  -c /usr/local/openresty/nginx/conf/nginx.conf
systemctl is-active --quiet claw-edge.service
curl -fsS http://127.0.0.1:9145/metrics | grep -q '^edge_up 1$'
if [ "$EDGE_LOGS_ENABLED" = true ]; then
  systemctl is-active --quiet fluent-bit.service
fi
```

The installer renders nginx to
`/usr/local/openresty/nginx/conf/nginx.conf`; never install the template there
without rendering and never validate with a bare `nginx -t` because OpenResty
is not on the default PATH. Fail if `${FB_*}` remains or a `delivery_stream`
value is empty when `edge_logs=true`. Set `EDGE_LOGS_ENABLED=true` only for that
profile; otherwise set it to `false` and leave `FIREHOSE_DELIVERY_STREAM` empty.

Always promote the verified nginx source to `deployment/edge/nginx.conf`. When
`edge_logs=true`, also promote the verified logging sources to the exact S3 keys
used by customer userdata: `deployment/edge/fluent-bit/install-fluent-bit.sh`,
`deployment/edge/fluent-bit/edge/*`, and
`deployment/observability/fluent-bit/edge/*`. Record each prior VersionId or
backup object first.

For future Edge instances, decode the current Edge LT userdata and change only
the `install-edge.sh` invocation to pass:

```text
LOGGING_ENABLED=true
ASSETS_BUCKET=<customer-assets-bucket>
AWS_REGION=<customer-region>
FIREHOSE_DELIVERY_STREAM=<customer-edge-stream>
```

Create a new LT version from the ASG's currently pinned numeric version while
preserving every other LT field and tag. Point the Edge ASG to the new numeric
version. Do not refresh running Edge instances; they were already hot-fixed.

When `user_hooks=false`, do not touch the host LT. A configured user hook needs
separate review of its S3 URI, SHA-256, timeout, failure policy, host-role IAM,
and rendered host userdata.

## Step 6 - Monitoring

Only when `monitoring=true`. This patch updates an existing Prometheus/Grafana
deployment; it does not bootstrap the monitoring stack. Before any monitoring
write, require all of these on the remote node:

```bash
test -f /opt/monitoring/.env
test -f /opt/monitoring/docker-compose.prom-grafana.yml
test -d /opt/monitoring/targets
test -d /opt/monitoring/grafana/provisioning
command -v docker
cd /opt/monitoring
docker compose --env-file .env -f docker-compose.prom-grafana.yml ps \
  --status running prometheus | grep -q prometheus
```

If any check fails, stop the `monitoring` profile without changing SGs or files.
Deploy the complete customer monitoring stack separately, then rerun this
profile. A node containing only `/opt/monitoring/.env` is not a deployed
monitoring stack.

After the preflight passes, copy `host-scripts/monitoring/prometheus.yml` to the
remote monitoring node, replace the region with the customer region, and
validate remotely:

```bash
cd /opt/monitoring
docker compose --env-file .env -f docker-compose.prom-grafana.yml config
docker compose --env-file .env -f docker-compose.prom-grafana.yml \
  exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose --env-file .env -f docker-compose.prom-grafana.yml \
  kill -s SIGHUP prometheus
curl -fsS http://127.0.0.1:9090/-/ready
```

Confirm active `openclaw-host-agent-ec2` and `openclaw-edge-nginx` targets.

## Step 7 - Verification And Exact Teardown

Read-only checks:

- `/tenants`, `/hosts`, and enabled `/tenants-stats` return authenticated JSON;
- API alias, API `$LATEST`, and the lifecycle consumer have the intended code;
- the lifecycle event-source mapping returned to its captured enabled state;
- all four selected GSIs are `ACTIVE`;
- stats writer has reserved concurrency `1` and table item `id=current`;
- every host serves 8899 metrics;
- when `host_logs=true`, every host has active Fluent Bit and a valid config;
- every Edge has active Fluent Bit, valid config, and 9145 metrics;
- Prometheus is ready and has host/Edge targets.

DNAT lifecycle test:

1. create one uniquely named tenant with normal production memory;
2. save the exact returned ID and poll to `running`;
3. verify exactly one DNAT at returned bitmap `host_port -> guest_ip:18789`;
4. verify no rule at `18789 + vm_num - 1`;
5. verify Redis `route:<id>` points to the same host/bitmap port;
6. record bitmap `used`;
7. delete that exact ID with `keep_data=false&skip_backup=true`;
8. poll to `deleted`;
9. verify both DNAT counts are zero, Redis is empty, bitmap `used` decreased by
   one, the exact VM directory is absent, and no Firecracker process references
   its socket.

Historical deployments may contain old DNAT residue. This patch prevents new
leaks and makes failed cleanup retryable, but must not blindly delete historical
rules. Inventory each exact `(host_port, guest_ip)` against DynamoDB, VM
directories, and Redis. Remove a rule only after customer approval. Never flush
the PREROUTING chain and never use a tenant-ID wildcard.

The following are separate acceptance cases, not permission to inject faults
into a customer production environment:

- validate `host_logs`, `edge_logs`, `monitoring`, and `user_hooks` only when
  their profile is selected;
- launch one fresh Edge instance from the updated LT before declaring
  `edge_logs` complete;
- perform the full reverse-order rollback on a disposable clone before using
  rollback as a production guarantee;
- run DNAT SSM timeout, Redis unavailable, missing `host_id`, and concurrent
  delete failure injection only in an isolated test deployment.

Rollback in reverse order using captured pre-state: restore ASG LT pointers,
Edge/host backups and S3 versions, API stage deployment, API and lifecycle
consumer zips, API alias and environment, and the lifecycle ESM state. Retain
tables, GSIs, secrets, and read-only IAM unless the customer separately approves
destructive removal.
