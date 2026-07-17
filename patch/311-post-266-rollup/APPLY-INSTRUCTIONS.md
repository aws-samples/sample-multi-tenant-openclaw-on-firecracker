# Applying Patch 311 (Post-266 Rollup) — CDK-free

Step-by-step guide for a human operator or a Claude Code executor. Follow the order — it
matters (fail-closed IAM first; running machines before future-machine sources; network
last and human-gated).

## Absolute rule: no `cdk deploy`

This deployment was CDK-deployed once and then **manually modified on the live system.**
Running `cdk deploy` or `setup.sh` now would overwrite those manual changes and break it.
**Never run either.** Every step below is a targeted AWS CLI / ssh command.

## Step 0 — Probe: gather the real values (read-only first)

```bash
aws sts get-caller-identity                              # region/account/identity
# Host IP(s) + SSH key path: <fill in>
# Is the API a PRIVATE or PUBLIC API Gateway? (decides if the private-API Lambda fix applies)
ssh -i <key> ubuntu@<host> 'journalctl -t claw-launch --no-pager -n 200'
ssh -i <key> ubuntu@<host> 'sudo tail -n 200 /var/log/openclaw-init.log'
curl -s "https://<api>/tenants" -H "x-api-key: <key>" | head
ssh -i <key> ubuntu@<host> "grep -o 's3://[^ ]*' /var/log/openclaw-init.log | sort -u"  # real S3 path
```

## Step 1 — Impact assessment (write before any change)

- **Affected:** <tenants / hosts / regions, from the probe evidence>
- **Symptom:** <rc=127 / 404 on private API / ABANDONED hosts / VPCE timeout — whichever you see>
- **Root cause:** per the README table.
- **Expected post-fix state:** <the concrete signal you'll verify in Step 6>

## Step 1.5 — Anti-revert hash gate + full change list (RUN BEFORE ANY WRITE)

This deployment is patched iteratively; a blind replace can DOWNGRADE it. `manifest.json`
lists every changed path with `base_hash` and `patch_hash`. For each shipped file, hash the
LIVE copy and branch:

```bash
LIVE=$(ssh -i <key> ubuntu@<host> 'sha256sum /home/ubuntu/launch-vm.sh' | awk '{print $1}')
# launch-vm.sh: base=51c7049b… patch=f2f95c20…   (init-host.sh: base=2b91afa5… patch=5e8f50c1…)
# known-intermediate (safe to overwrite): launch-vm.sh 376559e7… = the PRE-#312/#314 version of
#   this same 311 patch (hosts that applied the earlier 311 bundle). Upgrading it to f2f95c20…
#   is a forward fix (adds paired.json/gateway-token re-injection), NOT a revert.
```

- `LIVE == patch_hash` (`f2f95c20…`) → already applied, **SKIP**.
- `LIVE == base_hash` (`51c7049b…`) → clean apply, proceed.
- `LIVE == 376559e7…` (pre-#312/#314 311 build) → **known intermediate, proceed** — this
  upgrade adds the re-injection fix; it is forward, not a revert.
- `LIVE == none of the above` → **diverged. STOP.** Show `diff` to the terminal user; overwrite
  only on explicit approval (the live copy may be a newer fix — don't revert it).

## Step 2 — Hot-fix running machines (restore service now)

### 2a. Fail-closed prerequisite: IAM grant — FIRST. Probe with the HOST role, not yours.

Permissions: `iam:PutRolePolicy` (to grant); the probe runs on the host so it uses the host
instance role.

```bash
# Probe from the host (uses the EC2 host role — the identity launch-vm.sh actually uses):
ssh -i <key> ubuntu@<host> "aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{\"tenant_id\":{\"S\":\"__probe__\"}}' --region <region>"
# AccessDenied -> grant, then re-probe:
bash iam/apply-iam.sh <host-role-name> <region> <account-id>
```

(Your own admin creds reading the table proves nothing about the host role.)

### 2b. Replace launch-vm.sh on the live host (after the Step-1.5 gate cleared it)

Permissions: SSH key to the host. Back up, replace, `bash -n`, diff-guard, roll back.

```bash
ssh -i <key> ubuntu@<host> 'cp /home/ubuntu/launch-vm.sh /home/ubuntu/launch-vm.sh.bak.311'
scp -i <key> host-scripts/launch-vm.sh.patched ubuntu@<host>:/home/ubuntu/launch-vm.sh
ssh -i <key> ubuntu@<host> 'bash -n /home/ubuntu/launch-vm.sh && echo syntax-ok'
ssh -i <key> ubuntu@<host> 'diff /home/ubuntu/launch-vm.sh.bak.311 /home/ubuntu/launch-vm.sh'
# rollback: cp /home/ubuntu/launch-vm.sh.bak.311 /home/ubuntu/launch-vm.sh
```

## Step 3 — Lambda code (private-API routing + rebuild semantics)

Permissions: `lambda:ListFunctions`, `lambda:UpdateFunctionCode`. The private-API routing
fix is only needed on a private API Gateway (Step 0). See `lambda/APPLY-LAMBDA.md`.

## Step 4 — Future-machine source (S3 + Launch Template)

### 4a. S3-pulled launch-vm.sh — temp key, verify, promote (keep old version to roll back)

Permissions: `s3:PutObject`, `s3:GetObject` (+ `s3:GetObjectVersion` for rollback). Use the
cross-platform sha256 helper (a bare `shasum` fails on Linux/AL2023):

```bash
_sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"|awk '{print $1}';
  elif command -v shasum >/dev/null; then shasum -a 256 "$1"|awk '{print $1}';
  else echo FATAL-no-sha256 >&2; return 1; fi; }
REAL=$(ssh -i <key> ubuntu@<host> "grep -o 's3://[^ ]*launch-vm.sh' /var/log/openclaw-init.log" | head -1)
aws s3 cp host-scripts/launch-vm.sh.patched "${REAL}.311.tmp" --region <region>
aws s3 cp "${REAL}.311.tmp" /tmp/verify --region <region>
diff /tmp/verify host-scripts/launch-vm.sh.patched && bash -n /tmp/verify && echo promote-ok
aws s3 cp "${REAL}.311.tmp" "$REAL" --region <region>   # promote only after ok
```

### 4b. LT-baked init-host.sh

See `launch-template/APPLY-LT.md` — running hosts were hot-fixed already; future hosts need
a new LT version + `update-auto-scaling-group` (the ASG pins a version). NO cdk deploy.

## Step 5 — Network / VPCE (describe-only, human-gated)

**AI runs only the `describe` probes.** See `network/APPLY-NETWORK.md`: probe whether the
Secrets Manager VPCE is even needed (skip entirely if AOS isn't deployed or NAT egress
exists), and if it is, present the proposed `create-vpc-endpoint` to the terminal user and
STOP. Do not create/modify any network resource without explicit approval.

## Step 6 — Verify (dual path) + fresh-machine check + cleanup

**Fresh-machine validation:** because this patch touched the LT-baked init-host.sh, after
Step 4b launch ONE new host and confirm it registers healthy on its own (boots clean with
no hot-fix). If you only applied the S3 script, the hot-fixed live host suffices.

Verify BOTH paths (they log differently):

```bash
# normal path
curl -X POST "https://<api>/tenants" -H "x-api-key: <key>" -d '{"tenant_id":"patch-311-test"}'
# ~30s later on host: expect "using control-plane pre-minted gateway token", no rc=127
ssh -i <key> ubuntu@<host> 'journalctl -t claw-launch --no-pager -n 100 | grep -iE "pre-minted|rc=127"'
# recovery path (#312/#314) — actually reproduce the failure, don't just grep old logs.
# This is the whole point: prove paired.json is REBUILT after the data disk lost it.
TID=patch-311-test
# a) prove the pre-fix failure mode is reachable: blank the on-disk paired.json, then kill FC.
#    (the mount path is host-specific; find it under the tenant's data mount — devices/paired.json)
ssh -i <key> ubuntu@<host> "sudo truncate -s 0 \$(sudo find /mnt /srv -path '*'$TID'*/.openclaw/devices/paired.json' 2>/dev/null | head -1) || true"
ssh -i <key> ubuntu@<host> "sudo pkill -f \"firecracker.*$TID\""
# b) host-agent relaunches with 4 args (NEW_DATA=false). Watch for the re-inject to fire:
sleep 30
ssh -i <key> ubuntu@<host> "journalctl -t claw-launch --since '-2min' --no-pager | grep -iE 'DDB fallback|re-inject|paired'"
# c) confirm paired.json is non-empty again and the gateway reports the device PAIRED:
ssh -i <key> ubuntu@<host> "sudo cat \$(sudo find /mnt /srv -path '*'$TID'*/.openclaw/devices/paired.json' 2>/dev/null | head -1) | jq 'keys'"
# then in the frontend / gateway status: the tenant is PAIRED, not NOT_PAIRED.
# private API: a non-/ping route responds
curl -s "https://<api>/tenants" -H "x-api-key: <key>" | head
# cleanup
curl -X DELETE "https://<api>/tenants/patch-311-test" -H "x-api-key: <key>"
```
