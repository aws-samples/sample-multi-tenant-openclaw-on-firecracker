# Patch 315 — Concurrent-Dispatch Rollup — Apply Guide (CDK-free)

For a deployment that applied **only** `patch/266-token-drift-fix`. This one patch rolls up
all post-266 fixes (#290/#298/#300/#303/#304/#305/#306/#307/#312/#314) **plus #315**
(concurrent-tenant dispatch: remove the host-level inflight gate in ddb mode + host-agent
reconciler race fixes + scaler idle auto-scale-down off).

**How to read this doc:** every command below was verified in our test environment. Your
environment differs (resource names, host count, paths) — so treat the commands as the
tested recipe and **adapt the placeholders** (`<region>`, `<host>`, `<key>`, `<api>`,
`<assets-bucket>`, role/ASG/LT names) to your deployment. `manifest.json` lists exactly which
files/functions get replaced — that is also your **minimal backup set** (nothing outside it
is touched).

## Absolute rules

- **No `cdk deploy` / no `setup.sh`.** This deployment was CDK-deployed once and then
  hand-modified; a stack deploy would overwrite those changes. Every step is a targeted CLI /
  ssh command.
- **Lambda: `update-function-code` ONLY.** That API replaces only code bytes. It does NOT
  change env / timeout / memory / layers / VPC / role (that's `update-function-configuration`)
  and does NOT touch the SQS event source mapping (queue binding / batch size / maxConcurrency).
  So configs and dispatch wiring stay exactly as-is. **Never run `update-function-configuration`,
  never touch the ESM.**
- **Only apply the layers your system actually needs** — decide by the Step 0 probes, not by
  assumption.

---

## Step 0 — Confirm the problems are real on THIS system (read-only; apply a layer only if its probe hits)

Don't apply blindly. Each probe is read-only; the verdict decides whether that layer is needed.

```bash
REGION=<region>; API="https://<api>"; KEY="<api-key>"

# (a) #315 concurrent dispatch — fingerprint = tenants stuck creating + assignment failed while tenant running.
#     (read-only; do NOT burst-create 30 tenants here — the fix isn't in yet, you'd strand them.)
aws dynamodb scan --table-name openclaw-tenants --region $REGION \
  --filter-expression '#s = :c' --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":c":{"S":"creating"}}' --select COUNT
# and the 15/300 fingerprint — assignments failed whose tenant is actually running:
#   scan the assignments table for status=failed, cross-check those tenant_ids are 'running' in tenants.
#   Any old 'creating' or failed-but-running pair => #315 is biting. dispatch.mode=ddb => this layer applies.

# (b) #298 private API — a non-/ping route returning 404 = bug present.
curl -s -o /dev/null -w 'GET /tenants -> %{http_code}\n' "$API/tenants" -H "x-api-key: $KEY"
#   404 => #298 present (apply Lambda). 200/403 => already fixed or not applicable.

# (c) #306 rc=127 / (d) #300 boot hang — log fingerprints on a host:
ssh -i <key> ubuntu@<host> 'journalctl -t claw-launch --no-pager | grep -iE "rc=127|log: command not found" | tail'   # hits => #306
ssh -i <key> ubuntu@<host> 'grep -n "installing tools + firecracker" /var/log/openclaw-init.log'                        # then check if boot stalled ~5min => #300

# (e) IAM prereq — does the HOST role already have the tenant-secrets read? (probe with the host role, not admin)
ssh -i <key> ubuntu@<host> "aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{\"tenant_id\":{\"S\":\"__probe__\"}}' --region $REGION"
#   AccessDenied => apply Step 2. Returns {} or an item => already granted, skip Step 2.

# discover your real S3 scripts path (used in Steps 1/3):
ssh -i <key> ubuntu@<host> "grep -o 's3://[^ ]*deployment/scripts' /var/log/openclaw-init.log | head -1"
```

---

## Step 1 — Backup the minimal set (only what manifest.json replaces) — BEFORE any write

Back up exactly the files/functions this patch replaces. Two Lambda backup methods, **do BOTH**:

- **(A) publish a pre-patch version — the primary rollback anchor.** `publish-version` freezes
  the function's CURRENT code+config into an immutable version number. This is the cleanest
  rollback: for a function fronted by an alias you just point the alias back at that version.
  A published version also keeps a downloadable copy of that code forever.
- **(B) download the current `$LATEST` code zip — the fallback.** Because the SQS/EventBridge
  functions (and the dispatch path of `openclaw-api`) run on **`$LATEST`, not on any alias**,
  rolling _them_ back means re-deploying the exact prior bytes to `$LATEST`. The download is
  that source, and it also covers the case where `$LATEST` is _ahead_ of every published
  version (a "bare $LATEST" that was never published — this really happens; see the note).

> **Why both (real-world reason):** in a live deployment `$LATEST` can already be ahead of the
> alias — someone ran `update-function-code` without publishing. Then the alias version does
> NOT capture what the dispatch/$LATEST path is actually running, and only the download (B)
> preserves it. So publish an anchor AND download the current $LATEST.

```bash
BK=~/Downloads/315-concurrent-dispatch-rollup/backup; mkdir -p "$BK"/{lambda,hosts,s3}

for FN in openclaw-api openclaw-lifecycle-consumer openclaw-scaler; do
  # (A) publish a pre-patch anchor version (immutable snapshot of current $LATEST code+config)
  ANCHOR=$(aws lambda publish-version --function-name "$FN" --region $REGION \
    --description "pre-patch-315 rollback anchor" --query Version --output text) \
    || { echo "STOP: cannot publish anchor for $FN"; exit 1; }
  echo "$FN pre-patch anchor version = $ANCHOR" | tee -a "$BK/lambda/anchors.txt"

  # record whatever alias exists (name may NOT be 'live' — discover it, don't assume):
  aws lambda list-aliases --function-name "$FN" --region $REGION \
    --query 'Aliases[].{Name:Name,Version:FunctionVersion}' --output json > "$BK/lambda/$FN.aliases.json" 2>/dev/null || echo '[]' > "$BK/lambda/$FN.aliases.json"

  # (B) download the current $LATEST bytes (fallback + the only source for a $LATEST rollback)
  aws lambda get-function --function-name "$FN" --region $REGION > "$BK/lambda/$FN.get-function.json" \
    || { echo "STOP: $FN not found"; exit 1; }
  URL=$(python3 -c "import json;print(json.load(open('$BK/lambda/$FN.get-function.json'))['Code']['Location'])")
  curl -fsSL "$URL" -o "$BK/lambda/$FN.code.zip" && unzip -tq "$BK/lambda/$FN.code.zip" >/dev/null \
    || { echo "STOP: $FN code download failed/invalid — irreversible without it, abort"; exit 1; }
done

# 1b. Host scripts — the exact bytes we overwrite on each host.
for h in <host1> <host2>; do
  mkdir -p "$BK/hosts/$h"
  scp -i <key> "ubuntu@$h:/home/ubuntu/launch-vm.sh" "$BK/hosts/$h/launch-vm.sh"
  ssh -i <key> "ubuntu@$h" 'sudo cat /opt/openclaw/host-agent.py' > "$BK/hosts/$h/host-agent.py"
done

# 1c. Current S3 scripts (what future hosts pull today) — so the S3 promote is reversible.
BASE=<s3://...deployment/scripts>          # from Step 0
for f in launch-vm.sh host-agent.py; do aws s3 cp "$BASE/$f" "$BK/s3/$f" --region $REGION || true; done
```

> Not backed up (not changed by this patch): ASG config, current LT version/UserData (Step 6
> only ADDS a new LT version, it doesn't edit the existing one), DynamoDB data, security groups.

---

## Step 2 — IAM prerequisite (only if Step 0e returned AccessDenied) — fail-closed, do FIRST

The launch-vm token fallback reads `openclaw-tenant-secrets` with the host role; a denied read
aborts the launch. Grant it before the new launch-vm.sh. Patch-specific policy name so it can't
collide with a customer policy; idempotent.

```bash
ROLE=<host-role-name>; ACCOUNT=<account-id>
aws iam put-role-policy --role-name "$ROLE" --policy-name patch-315-tenant-secrets-read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"Patch315\",\"Effect\":\"Allow\",\"Action\":\"dynamodb:GetItem\",\"Resource\":\"arn:aws:dynamodb:$REGION:$ACCOUNT:table/openclaw-tenant-secrets\"}]}"
# verify (re-run Step 0e — should no longer be AccessDenied).
# rollback: aws iam delete-role-policy --role-name "$ROLE" --policy-name patch-315-tenant-secrets-read
```

---

## Step 3 — Update the S3 scripts source (BEFORE the Lambda step)

Hosts pull `launch-vm.sh` / `host-agent.py` from S3 at boot (`init-host.sh`). Update S3 **before**
the Lambda step so that any host that boots (ASG replacement / health-recovery / manual
scale-out) during the rollout gets the new #315 host code — not old code paired with a new
Lambda that's already allowing higher concurrency.

```bash
BASE=<s3://...deployment/scripts>
for f in launch-vm.sh host-agent.py; do
  aws s3 cp host-scripts/${f}.patched "$BASE/${f}.tmp" --region $REGION      # upload to temp key
  aws s3 cp "$BASE/${f}.tmp" /tmp/verify.$f --region $REGION
  diff -q /tmp/verify.$f host-scripts/${f}.patched >/dev/null \
    && { aws s3 cp "$BASE/${f}.tmp" "$BASE/$f" --region $REGION; aws s3 rm "$BASE/${f}.tmp" --region $REGION; echo "$f promoted"; } \
    || echo "STOP: $f round-trip mismatch — not promoting"
done
# rollback: aws s3 cp $BK/s3/<f> "$BASE/<f>"
```

---

## Step 4 — Hot-fix the running hosts (per host; restore service now)

For EACH live host. Atomic replace (temp file + `mv`) so a launch firing mid-copy never reads a
half-written file.

```bash
# 4a. launch-vm.sh
ssh -i <key> ubuntu@<host> 'cp /home/ubuntu/launch-vm.sh /home/ubuntu/launch-vm.sh.bak.315'
scp -i <key> host-scripts/launch-vm.sh.patched ubuntu@<host>:/home/ubuntu/launch-vm.sh.315.new
ssh -i <key> ubuntu@<host> 'bash -n /home/ubuntu/launch-vm.sh.315.new && mv /home/ubuntu/launch-vm.sh.315.new /home/ubuntu/launch-vm.sh'
```

**4b. host-agent.py — set `KillMode=process` FIRST, then replace + restart.**

> **Why this is mandatory (not optional):** `host-agent.service` ships with no `KillMode`, so
> systemd defaults to `control-group`. Every tenant firecracker is started by host-agent
> (`subprocess` / `nohup … & disown` — neither leaves the service cgroup), so a plain
> `systemctl restart host-agent` SIGTERMs the **whole cgroup and kills every running tenant VM
> on that host.** In the #315 burst scenario (host full of VMs) that's the worst time.
>
> `KillMode=process` makes systemd signal only the main process (host-agent itself). The
> tenant firecrackers survive the restart, and host-agent's own design re-adopts them on the
> next tick: `_probe_all` sees each FC still running (`pgrep -f "api-sock …"`) and leaves it
> alive — this is the reconcile-and-reuse model the code already assumes. So `KillMode=process`
> doesn't bypass the design; it makes systemd match it. (Rollback of in-memory state that #315
> wants still happens — the restart clears `_recovering` and rebuilds from vm.json/DDB.)

```bash
# set the drop-in and PROVE it took (don't assume):
ssh -i <key> ubuntu@<host> 'sudo mkdir -p /etc/systemd/system/host-agent.service.d && \
  printf "[Service]\nKillMode=process\n" | sudo tee /etc/systemd/system/host-agent.service.d/killmode.conf && \
  sudo systemctl daemon-reload && systemctl show host-agent -p KillMode'
#   must print: KillMode=process   (if not, STOP — a restart would kill tenant VMs)

# count running FC before, replace the agent, restart, count after — FC_AFTER must be >= FC_BEFORE.
FC_BEFORE=$(ssh -i <key> ubuntu@<host> 'pgrep -fc firecracker'); echo "FC before: $FC_BEFORE"
ssh -i <key> ubuntu@<host> 'sudo cp /opt/openclaw/host-agent.py /opt/openclaw/host-agent.py.bak.315'
scp -i <key> host-scripts/host-agent.py.patched ubuntu@<host>:/tmp/host-agent.py.315
ssh -i <key> ubuntu@<host> 'python3 -m py_compile /tmp/host-agent.py.315 && sudo install -m 0644 /tmp/host-agent.py.315 /opt/openclaw/host-agent.py'
ssh -i <key> ubuntu@<host> 'sudo systemctl restart host-agent; sleep 3; systemctl is-active host-agent'
FC_AFTER=$(ssh -i <key> ubuntu@<host> 'pgrep -fc firecracker'); echo "FC after: $FC_AFTER (was $FC_BEFORE)"
#   FC_AFTER >= FC_BEFORE => no tenant VM was killed. If it dropped, the drop-in didn't take.
# rollback: sudo cp /opt/openclaw/host-agent.py.bak.315 /opt/openclaw/host-agent.py && sudo systemctl restart host-agent
```

> Repeat 4a+4b on **every** host and confirm each `/opt/openclaw/host-agent.py` hashes to
> `a7daa3ce…` afterward. A mixed fleet (new Lambda + an un-updated agent) can hand concurrent
> work to an agent that still leaks `_recovering`.

---

## Step 5 — Update the three Lambda functions (update-function-code only)

Full source is shipped under `lambda/api/` (36 files) + `lambda/scaler/handler.py` (gateway HEAD
snapshot). Save the block below as a script and run it from the patch's `lambda/` dir.

- **openclaw-api**: API Gateway calls it through an **alias** (commonly named `live`, but
  **do NOT assume** — discover it, some deployments use a different alias or none). The dispatch
  SQS ESM is bound to its **`$LATEST`**. So the moment you `update-function-code`, the dispatch
  path runs the new #315 code (before/independent of the alias). Update code → publish → move
  whatever alias fronts the API.
- **openclaw-lifecycle-consumer** (`$LATEST`): this customer has `create_via_queue=false`, so
  **create does NOT go through the consumer** — it handles start/stop/restart/rebuild/delete.
  It must still be updated (same `api/` source) for the #303/#304/#305 rebuild + #263 delete
  semantics. **Verify it via a rebuild/delete, not via create.**
- **openclaw-scaler** (`$LATEST`): separate source-only zip; carries #315 idle-off.

```bash
set -euo pipefail
LAMBDA_DIR="$(cd "$(dirname "$0")" && pwd)"; REGION=<region>
BK=~/Downloads/315-concurrent-dispatch-rollup/backup

# Discover the alias fronting openclaw-api (do NOT hardcode 'live'). Empty => API GW may call
# $LATEST directly; then there's no alias to move and rollback is purely $LATEST re-deploy.
API_ALIAS=$(aws lambda list-aliases --function-name openclaw-api --region $REGION \
  --query 'Aliases[0].Name' --output text 2>/dev/null); [ "$API_ALIAS" = "None" ] && API_ALIAS=""
ROLLBACK_VER=$([ -n "$API_ALIAS" ] && aws lambda get-alias --function-name openclaw-api --name "$API_ALIAS" --region $REGION --query FunctionVersion --output text || echo "")
echo "openclaw-api alias='${API_ALIAS:-<none>}' currently -> version '${ROLLBACK_VER:-N/A}'"
echo "pre-patch anchor versions (from Step 1) are in $BK/lambda/anchors.txt — that is your durable rollback point"

# build api zip WITH deps — MUST be ARM64/manylinux wheels (cryptography has native code).
# Simplest reliable way: run this ON AN arm64 host (bastion) so `pip install` gives native wheels.
# Cross-platform alternative (any host): use --platform manylinux2014_aarch64 as below.
# EITHER WAY the goal is deps that match the Lambda arm64 runtime — a source-only or wrong-arch
# zip fails cold start with "Unable to import module".
rm -rf /tmp/api-build && mkdir -p /tmp/api-build
pip install --no-cache-dir --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --upgrade -r "$LAMBDA_DIR/api/requirements.txt" -t /tmp/api-build
cp -a "$LAMBDA_DIR/api/." /tmp/api-build/
( cd /tmp/api-build && zip -qr /tmp/api-lambda.zip . )
unzip -l /tmp/api-lambda.zip | grep -qi aws_lambda_powertools || { echo "STOP: deps not bundled"; exit 1; }

# openclaw-api: update $LATEST (no --publish), wait, publish once, move the alias (if any)
aws lambda update-function-code --function-name openclaw-api --zip-file fileb:///tmp/api-lambda.zip --region $REGION >/dev/null
aws lambda wait function-updated --function-name openclaw-api --region $REGION
NEW_VER=$(aws lambda publish-version --function-name openclaw-api --region $REGION --query Version --output text)
[ -n "$API_ALIAS" ] && aws lambda update-alias --function-name openclaw-api --name "$API_ALIAS" --function-version "$NEW_VER" --region $REGION >/dev/null

# consumer: same api zip, $LATEST
aws lambda update-function-code --function-name openclaw-lifecycle-consumer --zip-file fileb:///tmp/api-lambda.zip --region $REGION >/dev/null
aws lambda wait function-updated --function-name openclaw-lifecycle-consumer --region $REGION

# scaler: own zip, $LATEST
rm -f /tmp/scaler.zip && ( cd "$LAMBDA_DIR/scaler" && zip -qr /tmp/scaler.zip handler.py )
aws lambda update-function-code --function-name openclaw-scaler --zip-file fileb:///tmp/scaler.zip --region $REGION >/dev/null
aws lambda wait function-updated --function-name openclaw-scaler --region $REGION

# confirm all THREE CodeSha256 changed vs backup (a missed one = fix half-applied)
for FN in openclaw-api openclaw-lifecycle-consumer openclaw-scaler; do
  NOW=$(aws lambda get-function --function-name $FN --region $REGION --query 'Configuration.CodeSha256' --output text)
  WAS=$(python3 -c "import json;print(json.load(open('$HOME/Downloads/315-concurrent-dispatch-rollup/backup/lambda/$FN.get-function.json'))['Configuration']['CodeSha256'])")
  [ "$NOW" != "$WAS" ] && echo "$FN updated OK" || { echo "STOP: $FN CodeSha256 unchanged — not updated"; exit 1; }
done
```

**Rollback:** see the dedicated "Rollback the control-plane Lambdas" section below —
openclaw-api needs BOTH its alias path and its `$LATEST`/dispatch path reverted.

---

## Step 6 — Future machines (only if #300 probe hit; NO cdk deploy)

`init-host.sh` is baked into LT `openclaw-host-lt` UserData, not pulled from S3. Running hosts
already booted — this only matters for future scale-out, and only if Step 0d showed #300.
Create a NEW LT version carrying the patched `init-host.sh` and point the ASG at it; do **not**
edit the existing version, and do **not** trigger an instance refresh (that would replace live
hosts). New scale-outs pick it up; existing hosts are untouched. (Full render/`update-auto-scaling-group`
commands: this is optional for this customer — skip unless #300 is confirmed.)

---

## Step 7 — Verify the new Lambda code is actually LIVE, then that the fixes work

Two layers of verification. **7a proves the new code is what's serving** (a green deploy ≠
the alias/$LATEST actually route to it). **7b proves the fixes behave.** All of 7a and the
7b invoke-based checks below were run in a same-architecture test environment before shipping.

### 7a — the new code is the code being served

```bash
REGION=<region>; BK=~/Downloads/315-concurrent-dispatch-rollup/backup

# (i) all three functions' deployed CodeSha256 changed vs the pre-patch backup (else half-applied)
for FN in openclaw-api openclaw-lifecycle-consumer openclaw-scaler; do
  NOW=$(aws lambda get-function --function-name $FN --region $REGION --query 'Configuration.CodeSha256' --output text)
  WAS=$(python3 -c "import json;print(json.load(open('$BK/lambda/$FN.get-function.json'))['Configuration']['CodeSha256'])")
  [ "$NOW" != "$WAS" ] && echo "$FN code changed OK" || { echo "STOP: $FN unchanged — not updated"; exit 1; }
done

# (ii) the alias (if any) points at the newly published version — this is the API-GW path
API_ALIAS=$(aws lambda list-aliases --function-name openclaw-api --region $REGION --query 'Aliases[0].Name' --output text 2>/dev/null); [ "$API_ALIAS" = "None" ] && API_ALIAS=""
[ -n "$API_ALIAS" ] && echo "openclaw-api alias '$API_ALIAS' -> v$(aws lambda get-alias --function-name openclaw-api --name "$API_ALIAS" --region $REGION --query FunctionVersion --output text)"

# (iii) the code actually cold-starts (no dependency/import breakage) — invoke a read-only route.
#       200 + no FunctionError + no 'Unable to import' in logs = the repackaged deps load fine.
#       (this is the key check for the "reuse existing deps + overlay patched source" packaging.)
cat > /tmp/ping.json <<'E'
{"resource":"/ping","path":"/ping","httpMethod":"GET","headers":{},"requestContext":{"resourcePath":"/ping","httpMethod":"GET"}}
E
aws lambda invoke --function-name openclaw-api --qualifier "${API_ALIAS:-\$LATEST}" \
  --payload fileb:///tmp/ping.json /tmp/ping.out --region $REGION --query '{StatusCode:StatusCode,FunctionError:FunctionError}' --output json
aws logs filter-log-events --log-group-name /aws/lambda/openclaw-api --region $REGION \
  --start-time $(python3 -c "print(($(date +%s)-120)*1000)") \
  --filter-pattern 'Unable OR ImportError OR ModuleNotFound' --query 'events[].message' --output text | head
#   FunctionError=null + empty log filter => new code loads & runs. (Verified this way pre-ship.)
```

### 7b — the fixes behave (business-level)

```bash
API="https://<api>"; KEY="<api-key>"; export RUN="p315v-<initials>-<YYYYMMDDHHMM>"
# NOTE: tenant_id/name must be DNS-safe: ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ (lowercase/digits/hyphen).
IDS=(); for n in $(seq -w 1 30); do IDS+=("${RUN}-${n}"); done   # zero-padded so ...-01 can't prefix-match ...-10

# SAFETY: none of these ids may already exist (never reuse a real tenant id).
abort=""; for t in "${IDS[@]}"; do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$API/tenants/$t" -H "x-api-key: $KEY")" = "404" ] || { echo "taken: $t"; abort=1; }
done; [ -n "$abort" ] && { echo "STOP: pick a fresh RUN"; exit 1; }

# #315: burst-create; all must reach running, none stuck creating. (create returns 202 'queued'
# with a suffixed id, e.g. <id>-0426 — track the real ids from the responses.)
for t in "${IDS[@]}"; do curl -s -X POST "$API/tenants" -H "x-api-key: $KEY" -d "{\"tenant_id\":\"$t\",\"name\":\"$t\"}" >/dev/null & done; wait
sleep 180
curl -s "$API/tenants" -H "x-api-key: $KEY" | python3 -c "import sys,json,os;r=os.environ['RUN'];d=json.load(sys.stdin);print({t.get('name'):t.get('status') for t in d.get('tenants',[]) if str(t.get('name','')).startswith(r)})"
#   expect all running; no assignment left failed while its tenant is running.

# #298: non-/ping route responds (not 404)
curl -s -o /dev/null -w 'GET /tenants -> %{http_code}\n' "$API/tenants" -H "x-api-key: $KEY"

# consumer path (create_via_queue=false => consumer only runs on lifecycle ops, NOT create):
#   rebuild or delete ONE test tenant and confirm it goes through the lifecycle queue + new semantics.
#   (verified: delete returns 202 'queued' -> status 'deleting' -> consumer takes it to 'deleted'.)

# scaler idle-off: next run marks idle hosts but terminates none.
aws logs tail /aws/lambda/openclaw-scaler --since 10m --region $REGION | grep -iE "idle|terminate|IDLE_RECLAIM" | tail

# cleanup — delete ONLY our run's tenants (match by the DNS-safe id you created; exact, never a prefix).
```

---

## Rollback the control-plane Lambdas (if the new code misbehaves)

openclaw-api has **two invoke paths on different qualifiers**, so it needs BOTH reverted; the
`$LATEST`-only functions need a code re-deploy (no alias to flip). Use the Step-1 backups.

```bash
REGION=<region>; BK=~/Downloads/315-concurrent-dispatch-rollup/backup
API_ALIAS=$(aws lambda list-aliases --function-name openclaw-api --region $REGION --query 'Aliases[0].Name' --output text 2>/dev/null); [ "$API_ALIAS" = "None" ] && API_ALIAS=""

# openclaw-api — path 1: the alias (API Gateway). Point it back at the pre-patch anchor version
# (from $BK/lambda/anchors.txt), instant + lossless:
[ -n "$API_ALIAS" ] && aws lambda update-alias --function-name openclaw-api --name "$API_ALIAS" \
  --function-version <openclaw-api pre-patch anchor from anchors.txt> --region $REGION

# openclaw-api — path 2: the dispatch SQS ESM runs $LATEST, which the alias flip does NOT cover.
# Re-deploy the pre-patch $LATEST bytes so the dispatch path also reverts:
aws lambda update-function-code --function-name openclaw-api \
  --zip-file "fileb://$BK/lambda/openclaw-api.code.zip" --region $REGION
aws lambda wait function-updated --function-name openclaw-api --region $REGION

# consumer + scaler — $LATEST only, no alias: re-deploy their pre-patch zips.
for FN in openclaw-lifecycle-consumer openclaw-scaler; do
  aws lambda update-function-code --function-name $FN --zip-file "fileb://$BK/lambda/$FN.code.zip" --region $REGION
  aws lambda wait function-updated --function-name $FN --region $REGION
done

# confirm each function's CodeSha256 is back to the pre-patch value in $BK/lambda/<fn>.get-function.json.
```

Alternative for the alias path: instead of the anchor version you can `update-function-code`
$LATEST from `openclaw-api.code.zip`, then `publish-version` and point the alias at it — but the
pre-patch **anchor version** is cleaner (it's the exact pre-patch code+config, already immutable).

## Rollback the host / S3 / IAM layers

- **host scripts**: restore `.bak.315` on each host; `systemctl restart host-agent` (the
  `KillMode=process` drop-in can stay — it's a correctness fix, harmless to keep).
- **S3 scripts**: `aws s3 cp $BK/s3/<f> "$BASE/<f>"`.
- **IAM**: `aws iam delete-role-policy --role-name <role> --policy-name patch-315-tenant-secrets-read`.
- **LT** (only if you added a version in Step 6): point the ASG back at the prior version number.
- **consumer / scaler** (`$LATEST`, no alias): re-deploy their backup `.code.zip`.
- **S3 scripts**: `aws s3 cp $BK/s3/<f> "$BASE/<f>"`.
- **IAM**: `aws iam delete-role-policy --role-name <role> --policy-name patch-315-tenant-secrets-read`.
- **LT** (if you added a version): point the ASG back to the prior version number.
