# Applying `monitor-patch`

Source range:

- Base: `a547dc74fe25ea0219c804933c5a7da8af1e3b39`
- Patch: `e63bc7d9df85b4c8918a0612391be9cccf458186`
- Status: `MANUAL_REVIEW`

This is an environment-independent runbook for a human or customer-side coding
agent. Discover every resource in the customer account. Do not reuse resource
IDs, instance IDs, API keys, aliases, state files, or observations from another
environment.

The customer-side agent needs only this `patch/monitor-patch` directory from the
same Git revision. It must not call anything under `lib/`: those scripts are
operator/test helpers and are not part of this workflow. Start in the patch
directory and verify the required tools and artifacts:

```bash
export PATCH_DIR="$(pwd -P)"
test -f "$PATCH_DIR/manifest.json"
command -v aws jq curl zip unzip sha256sum
aws sts get-caller-identity
```

Use AWS CLI and SSM only. Do not run CDK or `setup.sh`: either can overwrite
customer changes made after the original deployment. No local Docker is needed.
If monitoring is enabled, Docker commands run only on the remote monitoring node.

For every write: print the exact command, capture the pre-state and rollback
command, ask the terminal user to type `APPLY`, then execute and verify.

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
  --query 'Code.Location' --output text | xargs curl -fsSL -o /tmp/api.before.zip
aws lambda list-aliases --function-name "$API_FN" --region "$REGION" \
  > /tmp/api-aliases.before.json

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

## Step 2 - Hot-Fix Running Hosts

Apply these full-file artifacts to every active Firecracker host:

| Artifact | Live destination | Future S3 key |
|---|---|---|
| `host-scripts/host-agent.py.patched` | `/opt/openclaw/host-agent.py` | `deployment/scripts/host-agent.py` |
| `host-scripts/route_ops.py.patched` | `/opt/openclaw/route_ops.py` | `deployment/scripts/route_ops.py` |
| `host-scripts/launch-vm.sh.patched` | `/home/ubuntu/launch-vm.sh` | `deployment/scripts/launch-vm.sh` |
| `host-scripts/stop-vm.sh.patched` | `/home/ubuntu/stop-vm.sh` | `deployment/scripts/stop-vm.sh` |
| `host-scripts/migrate-vm.sh.patched` | `/home/ubuntu/migrate-vm.sh` | `deployment/scripts/migrate-vm.sh` |

For each artifact:

1. upload it to a temporary key in the customer's assets bucket;
2. verify the temporary object's SHA-256;
3. use SSM to copy it to `/tmp`, validate by type, back up the live file, and
   install mode `0644`;
4. promote the verified temporary object to the canonical S3 key;
5. retain the old S3 VersionId for rollback.

Build SSM parameters with `jq` so multiline commands are valid JSON:

```bash
# Set VALIDATOR to "python3 -m py_compile" for Python or "bash -n" for shell.
BACKUP="${DEST}.before-monitor-patch"
CHECKSUM_LINE="${TARGET_SHA}  /tmp/patch-file"
CMD=$(jq -nr \
  --arg src "$TEMP_S3_URI" \
  --arg region "$REGION" \
  --arg checksum_line "$CHECKSUM_LINE" \
  --arg validator "$VALIDATOR" \
  --arg dest "$DEST" \
  --arg backup "$BACKUP" \
  '[
    "set -eu",
    "aws s3 cp \($src | @sh) /tmp/patch-file --region \($region | @sh)",
    "echo \($checksum_line | @sh) | sha256sum -c -",
    "\($validator) /tmp/patch-file",
    "cp -a -- \($dest | @sh) \($backup | @sh)",
    "install -o root -g root -m 0644 /tmp/patch-file \($dest | @sh)"
  ] | join("\n")')
PARAMS=$(jq -nc --arg command "$CMD" '{commands:[$command],executionTimeout:["180"]}')
aws ssm send-command --instance-ids "$HOST_ID" --region "$REGION" \
  --document-name AWS-RunShellScript --parameters "$PARAMS"
```

Restart `host-agent` on both hosts and wait until `systemctl is-active host-agent` and
`curl -fsS http://127.0.0.1:8899/metrics` both pass.

## Step 3 - Lambda Overlay

Do not replace the customer's native dependencies. Download the live package,
overlay only patch source, and rezip:

```bash
mkdir -p /tmp/api-overlay/live
unzip -oq /tmp/api.before.zip -d /tmp/api-overlay/live

# Overlay these patch files at the same paths under /tmp/api-overlay/live:
# handler.py
# core/{auth.py,clients.py,pagination.py,ssm_dispatch.py}
# services/{fleet_service.py,host_service.py,registry_service.py,
#           tenant_query_service.py,tenant_service.py,tenant_stats_service.py}

(cd /tmp/api-overlay/live && zip -qr /tmp/api.patched.zip .)
REV=$(aws lambda get-function-configuration --function-name "$API_FN" \
  --region "$REGION" --query RevisionId --output text)
aws lambda update-function-code --function-name "$API_FN" --region "$REGION" \
  --revision-id "$REV" --zip-file fileb:///tmp/api.patched.zip
aws lambda wait function-updated --function-name "$API_FN" --region "$REGION"
```

Probe `$LATEST` before changing the serving alias. The event must include
`httpMethod`, `resource`, and `path`; success means Lambda metadata has no
`FunctionError` (a 404 response body for the synthetic path is acceptable).

Then publish a version. If API Gateway currently invokes an alias, update that
same alias. If it invokes `$LATEST`, do not invent an alias. If it invokes an
immutable numeric version, stop and have the customer approve an API integration
change to a new alias. Never silently point a different API at the function.

Repeat the overlay for `lambda/health_check/handler.py`. Use Lambda `DryRun` for
health-check verification because a real invocation can restart hosts.

## Step 4 - Optional Resources And API

### Tenant Query

Only when `tenant_query=true`:

1. create or adopt `openclaw/pagination-cursor`; its JSON `key` must base64url
   decode to exactly 32 bytes;
2. merge `PAGINATION_AES_KEY` into the live API environment using a JSON file;
3. create/adopt one GSI at a time and wait for `ACTIVE`:
   `gsi_tenant_user(tenant_user_id)`, `gsi_host(host_id)`,
   `gsi_status(status)`, `gsi_rootfs_version(q_rootfs_version)`;
4. backfill `q_rootfs_version` from valid `rootfs_version` values before creating
   its GSI;
5. only after all prerequisites pass, set `TENANT_QUERY_ENABLED=true`.

Every GSI is `ProjectionType=ALL`. Existing indexes must match exactly; do not
delete/recreate a mismatched customer index without separate approval.

### Tenant Stats

Only when `tenant_stats=true`:

1. create/adopt PAY_PER_REQUEST table `openclaw-tenant-stats`, key `id:S`, and
   enable PITR;
2. create/adopt the stats-writer Lambda role with CloudWatch Logs, tenants-table
   scan, stats-table read/write, and rootfs manifest S3 read;
3. package `lambda/tenant_stats/handler.py` as `handler.py`;
4. create/update `openclaw-tenant-stats-writer` as Python 3.12 arm64, timeout
   50, memory 8192, and reserved concurrency `1`;
5. create/adopt an enabled `rate(1 minute)` EventBridge rule and Lambda target;
6. grant the API role read access to the stats table and merge
   `TENANT_STATS_TABLE=openclaw-tenant-stats` into its live environment.

For an existing writer, verify architecture is already arm64. Do not pass
`--architectures` to `update-function-configuration`; that CLI operation does
not accept it.

### `/tenants-stats`, API Key, And Authorizer

Call the route with the real customer auth first:

- `200`: already complete, skip;
- `503`: route exists; finish stats resources;
- `403`: inspect API-key requirement/authorizer/resource policy;
- `404`: create the route.

For a new route, copy the exact method auth, `apiKeyRequired`, authorizer ID,
Lambda AWS_PROXY integration URI, and integration HTTP method from the existing
`GET /tenants` method. Add exact OPTIONS/CORS responses, create a deployment, and
move only the intended stage after confirming its deployment ID did not drift.

### Monitoring Network

Only when `monitoring=true`, describe the rules first and request explicit
approval for:

- VPC CIDR -> host SG TCP 8899;
- VPC CIDR -> Edge SG TCP 9145.

Do not add duplicate or public `0.0.0.0/0` metrics rules. Record only rules
created by this patch so rollback cannot remove pre-existing access.

## Step 5 - Host And Edge Fluent Bit

When `host_logs=true`, apply
`host-scripts/edge/fluent-bit/host/fluent-bit.conf` to every active Firecracker
host through a temporary S3 key and SSM. Back up
`/etc/fluent-bit/fluent-bit.conf`, install the verified file, run Fluent Bit
`--dry-run`, restart the service, and confirm it is active. Promote the same
verified file to
`deployment/observability/fluent-bit/host/fluent-bit.conf` for future hosts.

When `edge_logs=true`, apply these artifacts to all running Edge instances
through temporary S3 keys and SSM:

- `host-scripts/edge/nginx.conf`;
- `host-scripts/edge/install-fluent-bit.sh`;
- `host-scripts/edge/fluent-bit/edge/*`.

Render `fluent-bit.conf` with the customer's non-empty region and Firehose stream.
Fail if `${FB_*}` remains or the stream is empty. Run Fluent Bit `--dry-run`,
back up `/etc/fluent-bit`, install the files, restart Fluent Bit, and verify it is
active. Render nginx with the live Redis endpoint and instance private IP, run
`nginx -t`, reload, and verify `http://127.0.0.1:9145/metrics`.

Promote the same verified artifacts to the Edge S3 keys used by customer
userdata.

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

Only when `monitoring=true`. Copy
`host-scripts/monitoring/prometheus.yml` to the remote monitoring node, replace
the region with the customer region, and validate remotely:

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
- API alias and `$LATEST` have the intended code;
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

Rollback in reverse order using captured pre-state: restore ASG LT pointers,
Edge/host backups and S3 versions, API stage deployment, Lambda zip and alias,
and API environment. Retain tables, GSIs, secrets, and read-only IAM unless the
customer separately approves destructive removal.
