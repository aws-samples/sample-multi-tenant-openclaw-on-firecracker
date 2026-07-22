# Applying Patch 353-secret-ttl-plus-post315-rollup

Post-315 rollup for a deployment that already applied `patch/315-concurrent-dispatch-rollup`.
Rolls up the sync in gateway PR #84: **#353** secret-TTL removal · **#331** host launch-concurrency
gate · **#323** host-agent KillMode · **#330** capacity mem-dimension CAS · **#340** disk soft-gate ·
**#345** guest-log vsock · **#321** disk-leak GC · **#336** copy-file contract · **#343** rootfs_version
sync · **#338** pull-progress → Fluent Bit.

- `base_sha` (previous patch = 315) → `patch_sha` = the exact source in `manifest.json`.
- **status: MANUAL_REVIEW** — two operations need a human before you run them (#353 DDB-TTL disable
  on the live table; the ha_edge SG/edge review). Everything else is hot-applyable, NO CloudFormation redeploy.
- **stack `cdk`-based redeploy is FORBIDDEN** on this deployment (it was provisioned once via CDK then hand-modified; a
  deploy overwrites the manual state). Every stack change below is a manual CLI equivalent (no CloudFormation redeploy).

Transport note: hosts commonly sit in a private subnet (`api.mode=private`). Commands are written as
`ssh`/`scp` for readability; if SSH is blocked use the **SSM equivalent** (shell → `send-command`;
file push/pull → base64 via `send-command`). All hashes are **SHA-256**.

---

## Step 0 — DISCOVER: auto-probe the environment, then CONFIRM (don't hand-type these)

Run the read-only discovery tool FIRST — it senses the values you'd otherwise guess (the
LIVE control-plane API by behavior not name, which Lambda alias the API actually invokes vs
`$LATEST`, the SQS dispatch ESM target, the ASG-pinned LT version, live host ids, and a
per-fix applies_when verdict) and writes `environment.json`. Read its CONFIRM block and verify
every line before proceeding — this is how two different operators start from the SAME real
values instead of each filling a blank.

```bash
REGION=<region>; export AWS_DEFAULT_REGION=$REGION
bash lib/discover-env.sh "$REGION"        # READ-ONLY; writes environment.json + prints CONFIRM block
# Read the CONFIRM block. In particular verify (rule 9 + the 315 Lambda-link lesson):
#   - control-plane API = the non-proxy one with /tenants+/hosts; PROVE it with a host-SSM
#     GET /tenants that returns 200 (a /{proxy+} API named "-private" is the trap).
#   - the API invokes a specific Lambda ALIAS; the dispatch SQS ESM binds $LATEST — a code
#     update must hit BOTH, or it lands on a version nothing serves.
#   - the ASG's pinned LT version (NOT $Default); the live host ids; each fix's IN-SCOPE/CHECK verdict.
# If any line is wrong, STOP and fix your target/credentials. Only then use the values below.
```

Everything downstream reads from `environment.json` (API id, `lambda_link.api_invokes_alias`,
`asg.lt_version_pinned`, `hosts.instance_ids`) — no hand-typed resource names.

## Step 1 — Impact assessment (write before changing anything)

- **Who's affected**: all hosts (launch concurrency #331, KillMode #323, disk GC #321); all future
  tenant creates (capacity CAS #330, disk gate #340); every tenant's secret row (#353 TTL); log
  pipeline (#345/#338 — only if `logging.enabled==true`).
- **Symptom fixed**: burst recover crushing a host (#331); host-agent restart killing all tenant VMs
  (#323); mem oversell/OOM (#330); full-disk host still handed tenants (#340); orphan VM dirs filling
  /data (#321); rebuild/recover months later reading back a stale/mismatched token (#353).
- **Expected post-fix**: bursts queue (rate-limited, no crush); host-agent restart keeps tenant VMs;
  capacity honored on vcpu+mem; full-disk host deprioritized; delete removes the VM dir; the
  tenant-secret ciphertext persists so recover reads the original token.

## Step 1.5 — Full change list + anti-revert hash gate (RUN BEFORE ANY WRITE)

`manifest.json` `paths{}` lists every file + its `patch_sha256`. For each artifact you ship, confirm
it equals both the repo source at `patch_sha` and the shipped file (catches a mis-packaged patch):

```bash
# for a host-scripts/*.patched:  sha256sum host-scripts/launch-vm.sh.patched   == manifest patch_sha256
# for the live target before overwrite (record for rollback):
ssh $HOST 'sha256sum /home/ubuntu/launch-vm.sh'      # or: aws s3 cp <real-s3-path> - | sha256sum
```

## Step 2 — Hot-fix the RUNNING machines (restore service now)

Fail-closed prereq FIRST (rule 6): if the host role can't read `openclaw-tenant-secrets`, apply the
inline IAM in `iam/` before the token-dependent code. Probe with the HOST role (your admin creds
reading the table proves nothing):

```bash
# host-role probe (via SSM on the host, not your laptop):
aws dynamodb get-item --table-name openclaw-tenant-secrets --key '{"tenant_id":{"S":"__probe__"}}'  # AccessDenied -> apply iam/ first
```

**#331 launch-vm slots + #321 disk-GC + host-agent.py** (B-s3, all live hosts — full fleet per owner):

```bash
for H in $HOSTS; do
  IP=<resolve>; scp host-scripts/launch-vm.sh.patched   $IP:/home/ubuntu/launch-vm.sh
  scp host-scripts/host-agent.py.patched $IP:/opt/openclaw/host-agent.py
  scp host-scripts/migrate-vm.sh.patched $IP:/home/ubuntu/migrate-vm.sh
  ssh $IP 'grep -q OC_HOST_LAUNCH_SLOTS /etc/platform.env || echo OC_HOST_LAUNCH_SLOTS=30 | sudo tee -a /etc/platform.env'
  ssh $IP 'bash -n /home/ubuntu/launch-vm.sh && python3 -m py_compile /opt/openclaw/host-agent.py'  # validate by ext
done
```

**#323 KillMode drop-in** (A-lt live-host side; do NOT restart host-agent now — this restart would
still cgroup-kill under the old KillMode; the drop-in takes effect on the NEXT restart):

```bash
for H in $HOSTS; do IP=<resolve>
  ssh $IP 'sudo mkdir -p /etc/systemd/system/host-agent.service.d && \
    printf "[Service]\nKillMode=process\n" | sudo tee /etc/systemd/system/host-agent.service.d/killmode.conf && \
    sudo systemctl daemon-reload'   # daemon-reload only; NO restart
done
```

**#345 host-side reader + #338 fluent-bit** (B-s3; safe to apply before the guest side — a guest
without the forwarder just yields no logs, the reader listens and drops nothing critical):

```bash
for H in $HOSTS; do IP=<resolve>
  scp host-scripts/oc-guest-log-reader.py.patched $IP:/opt/openclaw/oc-guest-log-reader.py
  scp host-scripts/fluent-bit/*.conf host-scripts/fluent-bit/extract_tenant_id.lua $IP:/etc/fluent-bit/  # adapt path
done
```

## Step 3 — Fix the FUTURE-machine source (S3 + Launch Template)

**S3 (B-s3) — promote patched host scripts so new/rebooted hosts pull them** (upload to a temp key,
verify, promote; keep the old version-id for rollback):

```bash
for f in launch-vm.sh host-agent.py migrate-vm.sh oc-guest-log-reader.py; do
  aws s3 cp host-scripts/$f.patched s3://$ASSETS/deployment/scripts/$f   # record prior VersionId first
done
```

**Launch Template (A-lt) — #323 KillMode + #331 slot env into future hosts. USE `lib/apply-lt.sh`,
do NOT hand-wrangle base64** (it reads the ASG-pinned version, refuses raw `{{ }}` templates,
16KB-checks, and gates every mutation):

```bash
bash lib/apply-lt.sh pull  $LT $ASG $REGION      # -> /tmp/lt-$LT.cur.sh (rendered, no placeholders) + saves prior version
# edit /tmp/lt-$LT.cur.sh: apply ONLY this patch's init-host.sh hunk (host-agent.service now carries
#   KillMode=process; add OC_HOST_LAUNCH_SLOTS=30 to /etc/platform.env write; add the reader unit).
#   compare against launch-template/init-host.sh.patched for the exact hunk — keep all rendered values.
bash lib/apply-lt.sh push  $LT $ASG $REGION      # repack + create-launch-template-version + point ASG (gated)
bash lib/apply-lt.sh verify $ASG $REGION         # prints the 3-signal check for ONE new host
# rollback if a signal fails: bash lib/apply-lt.sh rollback $LT $ASG $REGION
```

## Step 4 — CDK stack changes → manual CLI equivalents (review-gated, NO CloudFormation redeploy)

**#353 disable DDB TTL on the live tenant-secrets table (`MANUAL_CLI_REVIEW` — human confirm first)**.
A template edit alone does NOT disable an already-enabled TTL; do it on the live table:

```bash
aws dynamodb describe-time-to-live --table-name openclaw-tenant-secrets --region $REGION   # before: ENABLED
aws dynamodb update-time-to-live --table-name openclaw-tenant-secrets \
  --time-to-live-specification 'Enabled=false,AttributeName=expires_at' --region $REGION
aws dynamodb describe-time-to-live --table-name openclaw-tenant-secrets --region $REGION   # after: DISABLED
```

**rollback_policy: RETAIN — never re-enable.** Re-enabling would sweep the now-retained ciphertext by
its old `expires_at` = data loss. This is a one-way, fail-safe change.

**#331/#330/#340 Lambda env (`AUTO_CLI`)** — merge (don't replace) the existing env:

```bash
aws lambda get-function-configuration --function-name openclaw-api --query Environment --output json > /tmp/env.json
# add DISPATCH_HOST_LAUNCH_CONCURRENCY=30 (+ any mem/disk-gate vars) to /tmp/env.json, then:
aws lambda update-function-configuration --function-name openclaw-api --environment file:///tmp/env.json --region $REGION
```

**ha_edge SG/edge (`MANUAL_CLI_REVIEW`)** — review the SG delta by hand; the host-agent.service
KillMode part ships via the Step-3 LT re-bake (not a separate SG change).

## Step 4.5 — Lambda code (C-lambda, overlay — reuse the LIVE package's deps, don't prebuild a zip)

The `lambda/api/` tree here is the full source at `patch_sha`. Overlay it onto the live package so
the platform-correct arm64 deps come from the customer's own function (requirements.txt unchanged):

```bash
aws lambda get-function --function-name openclaw-api --query Code.Location --output text | xargs curl -s -o /tmp/live.zip
mkdir /tmp/fn && cd /tmp/fn && unzip -q /tmp/live.zip
rm -rf core services routes consumers handler.py   # first-party dirs only; KEEP the dep dirs
cp -r <patch>/lambda/api/{core,services,routes,consumers,handler.py,requirements.txt} .
zip -qr /tmp/new.zip . && aws lambda update-function-code --function-name openclaw-api --zip-file fileb:///tmp/new.zip --region $REGION
aws lambda wait function-updated --function-name openclaw-api --region $REGION
# invoke-verify on $LATEST FIRST: FunctionError=None (a 404 body on a private API /ping is EXPECTED).
# NOW cover BOTH serving paths (environment.json lambda_link) — update-function-code only moved $LATEST:
#   • dispatch SQS ESM binds $LATEST  -> already covered by the update above.
#   • API GW invokes a specific ALIAS -> publish a version from the new $LATEST and repoint that alias,
#     else the HTTP path keeps serving the OLD code:
ALIAS=$(jq -r .lambda_link.api_invokes_alias environment.json | grep -oE '[^:]+$')   # the alias the API actually invokes
VER=$(aws lambda publish-version --function-name openclaw-api --region $REGION --query Version --output text)
aws lambda update-alias --function-name openclaw-api --name "$ALIAS" --function-version "$VER" --region $REGION
# rollback = redeploy the prior zip to $LATEST AND repoint $ALIAS back to its prior version (record both first).
```

## Step 5 — Post-fix: fresh-machine validation

Because Step 3 touched the LT/init-host + S3 scripts, launch **one** new host on the new LT and let it
boot clean (no hot-fix) — watch the 3 signals from `apply-lt.sh verify`. Only after it passes, run a
**controlled** instance-refresh (small MinHealthyPercentage), never a mass replace.

**#345 guest side — needs a NEW golden rootfs (prerequisite, not yet baked)**: the guest forwarder
lives in the read-only golden rootfs. To reach tenants: `build-rootfs.sh` (shipped as
`launch-template/build-rootfs.sh.patched`) bakes it → upload the new rootfs to S3 → host `pull-image`
swaps `openclaw-rootfs.ext4` → rebuild existing tenants (swaps the rootfs layer, PRESERVES the data
disk). Until then, the host-side reader is live and harmless (old guests simply emit no logs).

## Step 6 — Guided verification (a FALSIFIABLE check for EVERY fix; from `manifest.json` verifications[])

Run each `verifications[]` entry, gated by phase (A read-only always; B-lifecycle once; B-optional
when re-verifying a high-risk path). Judge by each entry's `pass_when`/`fail_when`. Highlights:

- **v-353-ttl-disabled** (A): `describe-time-to-live` → `DISABLED`.
- **v-353-token-consistency** (B): create → rebuild → get-credentials; KMS-decrypt the
  `openclaw-tenant-secrets` row == guest `openclaw.json .gateway.auth.token` (no openssl fallback).
- **v-331-burst-no-crush** (B): burst-create ~10/s on one host; all reach running (queued), host not
  crushed, 0 stuck creating.
- **v-323-killmode-survives** (B): with tenant VMs running, `systemctl restart host-agent`; firecracker
  process count unchanged (fail = count drops).
- **v-330-no-oversell** (B): burst near capacity; `used_vcpu<=cap AND used_mem_mb<=mem cap`, no OOM.
- **v-321-no-orphan-dir** (B): create then `DELETE ?keep_data=false`; `/data/firecracker-vms/<id>` gone.
- Use REAL table/field names, verified on a same-arch env (`openclaw-hosts`, `openclaw-assignments`).

## Step 7 — Precise teardown (one-to-one, zero wildcards)

Test tenants use a unique zero-padded prefix. Delete ONLY the exact ids returned at create (loop the
recorded list — NEVER a prefix glob; real hosts carry hundreds of `t-*`/`thr*` real tenants):

```bash
for id in "${CREATED_IDS[@]}"; do
  curl -s -X DELETE "$API/tenants/$id?keep_data=false"          # default keep_data=true is soft-delete
  # poll to deleted, then SSM-confirm /data/firecracker-vms/$id gone + no orphan fc; precise rm only if residual
done
# confirm real-tenant count identical before/after.
```
