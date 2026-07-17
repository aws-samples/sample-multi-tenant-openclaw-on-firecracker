# Applying Patch 311 (Post-266 Rollup)

Step-by-step guide to apply the post-266 fixes to a running OpenClaw on Firecracker
deployment. Follow the steps in order — the dependency order matters (a wrong order
fails mid-way). Suitable for a human operator or an AI assistant.

## Step 0: Gather information

```
1. Host IP(s) (metal instances) and SSH key path.
2. AWS region and account id.
3. The host instance-role name (if unknown, run `aws sts get-caller-identity`
   on the host and read it from the ARN).
4. Is your API a private API Gateway or public? (decides whether the private-API
   routing fix is needed).
5. Can you run `cdk deploy`? (yes -> a single deploy is simplest; no -> use the
   per-layer hotfix steps).
6. Any tenants stuck creating / unable to connect, or a stack stuck in ROLLBACK?
```

## Step 1: IAM grant (do this FIRST — fail-closed prerequisite)

`launch-vm.sh` reads the gateway token from `openclaw-tenant-secrets` on the
recovery path and **aborts if that read is denied**. Ensure the host role can read
that table before replacing scripts or rebuilding tenants.

Probe on the host:

```bash
aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{"tenant_id":{"S":"__probe__"}}' --region <region>
```

- Returns `{}` or an item -> already granted, skip this step.
- `AccessDeniedException` -> grant it:
  - If you will `cdk deploy` (Step 4), the grant is included — you can defer.
  - Otherwise apply the inline policy now:
    ```bash
    bash iam/apply-iam.sh <host-role-name> <region> <account-id>
    # Re-run the probe to confirm it no longer returns AccessDenied.
    ```

## Step 2: Host scripts

### 2a. Hot-replace on the current host (back up first, diff after)

If your deployment runs a customized `launch-vm.sh`, do not blind-replace: diff
first and confirm the only differences are the fixes; otherwise merge by hand.

```bash
ssh -i <key> ubuntu@<host> 'cp /home/ubuntu/launch-vm.sh /home/ubuntu/launch-vm.sh.bak.311'
scp -i <key> host-scripts/launch-vm.sh.patched ubuntu@<host>:/home/ubuntu/launch-vm.sh
ssh -i <key> ubuntu@<host> 'bash -n /home/ubuntu/launch-vm.sh && echo syntax-ok'
```

Roll back if anything looks wrong:
`ssh -i <key> ubuntu@<host> 'cp /home/ubuntu/launch-vm.sh.bak.311 /home/ubuntu/launch-vm.sh'`

### 2b. Future hosts: upload to S3

New hosts download these scripts at boot. Upload to the exact path the boot script
reads from — verify it, do not guess:

```bash
# Find the real S3 path this deployment pulls from:
grep -o 's3://[^ ]*launch-vm.sh' /var/log/openclaw-init.log
grep -o 's3://[^ ]*init-host.sh' /var/log/openclaw-init.log

# Upload (in the public repo the path is s3://<assets-bucket>/deployment/scripts/):
aws s3 cp host-scripts/launch-vm.sh.patched  <real-s3-launch-vm-path>  --region <region>
aws s3 cp host-scripts/init-host.sh.patched  <real-s3-init-host-path>  --region <region>

# Verify round-trip:
aws s3 cp <real-s3-launch-vm-path> /tmp/verify.sh --region <region>; bash -n /tmp/verify.sh && echo ok
```

## Step 3: Lambda code

See `lambda/APPLY-LAMBDA.md`. If you can `cdk deploy`, defer to Step 4 (it
repackages the Lambda). Otherwise update the API function code directly. The
private-API routing fix is only needed on private-API deployments (Step 0, Q4).

## Step 4: CDK deploy (IAM grant + VPC-endpoint toggle) — simplest one-shot

**Before deploying, check the Secrets Manager VPC endpoint** (see `cdk/APPLY-CDK.md`):

```bash
aws ec2 describe-vpc-endpoints --region <region> \
  --filters "Name=service-name,Values=com.amazonaws.<region>.secretsmanager" \
            "Name=vpc-id,Values=<vpc-id>" \
  --query 'VpcEndpoints[].[VpcEndpointId,PrivateDnsEnabled]' --output text
```

- If an endpoint with private DNS already exists, set
  `logging.aos.create_secretsmanager_vpce: false` in `config.yml` to reuse it —
  otherwise the deploy conflicts and rolls back.

Then deploy:

```bash
bash setup.sh <region> <profile-or-dash>    # pass "-" as the profile to use the instance role
# or: cdk deploy OpenClawOrchestrator --require-approval never -c region=<region>
```

A single deploy covers the IAM grant, the VPC-endpoint toggle, and the Lambda code.

## Step 5: Fix affected tenants / stacks

- **Stack still in ROLLBACK** (from a VPC-endpoint conflict): wait for rollback to
  finish, handle the endpoint per Step 4, then deploy again.
- **Tenants stuck creating** (from the token / permission issue): rebuild after the
  fix is in place:
  ```bash
  curl -X DELETE "https://<api>/tenants/<tid>" -H "x-api-key: <key>"; sleep 15
  curl -X POST   "https://<api>/tenants" -H "x-api-key: <key>" \
    -H "Content-Type: application/json" -d '{"tenant_id":"<tid>"}'
  ```

## Step 6: Verify

```bash
# Host: launch no longer exits rc=127 and no longer hangs at the tools step
ssh -i <key> ubuntu@<host> 'journalctl -t claw-launch --no-pager -n 100 | grep -iE "rc=127|DDB fallback"'

# Permission: the probe no longer returns AccessDenied
aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{"tenant_id":{"S":"__probe__"}}' --region <region>

# CDK: stack reaches CREATE_COMPLETE, no VPC-endpoint conflict
aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --region <region> --query 'Stacks[0].StackStatus'

# Private API: a non-/ping route responds
curl -s "https://<api>/tenants" -H "x-api-key: <key>" | head

# End to end: create a tenant, wait for status=running and app_health=up,
# then connect over WebSocket (token matches, no manual approve needed).
```
