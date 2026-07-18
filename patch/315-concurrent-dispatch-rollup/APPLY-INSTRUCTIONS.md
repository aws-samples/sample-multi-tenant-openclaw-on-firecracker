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
- **Host access: SSH may be blocked — use SSM if so.** The commands below are written as
  `ssh/scp -i <key> ubuntu@<host>` for readability, but in most deployments the hosts sit in a
  **private subnet with no inbound SSH** (this is the norm when `api.mode=private`), reachable
  only via **SSM**. If SSH doesn't work, run the SAME commands through SSM instead — no logic
  changes, just the transport:
  - a shell command `ssh … 'CMD'` → `aws ssm send-command --instance-ids <i-...> --document-name AWS-RunShellScript --parameters commands='["CMD"]' --region <region>` (then `get-command-invocation` for output).
  - a file push `scp local host:/path` → base64 the file and write it on the host via
    send-command: `B64=$(base64 -i local); ssm send-command … commands='["echo <B64> | base64 -d > /path"]'`.
  - a file pull (backup) → `ssm send-command` a `base64 /path`, read stdout from
    `get-command-invocation`, `base64 -d` locally.
    Use the host's real instance-id (from the ASG / `describe-instances`), not an IP.

---

## Step 0.0 — Verify the shipped artifacts are authentic (do this FIRST, before anything)

Confirm each shipped file equals its SOURCE in the repo — this catches a mis-packaged patch
(wrong file / wrong place) before you touch the live system. `manifest.json` → `replaces` lists,
per entry, the `source` (repo path), the `artifact` (the shipped file), and the `sha256` (that
file at `patch_sha`). **All hashes are SHA-256** — a review tool defaulting to SHA-1 will show a
false mismatch.

```bash
# from inside the patch dir. Each shipped artifact must hash to the manifest sha256:
shasum -a 256 host-scripts/launch-vm.sh.patched host-scripts/host-agent.py.patched \
              launch-template/init-host.sh.patched lambda/scaler/handler.py
#   expect: launch-vm.sh.patched=f73a601c…  host-agent.py.patched=a7daa3ce…
#           init-host.sh.patched=5e8f50c1…  scaler/handler.py=17f600d4…
# If you also have a gateway checkout at patch_sha, cross-check the SOURCE matches the artifact:
#   git show <patch_sha>:deploy/userdata/launch-vm.sh | shasum -a 256   # == f73a601c…
# api tree is a directory: confirm 36 files under lambda/api/ and that the #315 files are present
# (core/clients.py, core/dispatch/binpack.py, services/dispatch_poller.py, services/dispatch_service.py).
find lambda/api -type f ! -path '*__pycache__*' | wc -l   # expect 36
```

If any hash doesn't match (and it's genuinely SHA-256, not a SHA-1 false alarm) → **STOP**, the
patch is mis-packaged; do not apply.

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

# 1b. Host scripts — the exact bytes we overwrite on each host. Back up PER HOST (separate dir
#     per instance-id): hosts legitimately run DIFFERENT versions (one booted earlier, or was
#     hand-patched), so a fleet-wide drift is EXPECTED — don't be alarmed by mismatched hashes.
#     Per-host backup means each rolls back to its OWN prior version. The patch converges them
#     all to the target hash in Step 4, which intentionally flattens the drift.
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

> **If Step 0e already succeeded (no AccessDenied), the grant exists — SKIP, do NOT add a
> duplicate.** A 266 customer typically already has an equivalent grant under a DIFFERENT policy
> name (e.g. `openclaw-patch266-tenant-secrets-getitem`). Adding `patch-315-...` on top would
> just leave a second, identical statement — redundant, and clutter for whoever cleans up IAM
> later. Confirm equivalence, don't name-match: the grant is sufficient when, for the SAME
> `openclaw-tenant-secrets` table ARN, some attached policy allows `dynamodb:GetItem`
> (Effect=Allow) AND no policy Denies it (explicit Deny beats Allow). The Sid/policy NAME is
> irrelevant to authorization. Best proof is Step 0e itself: a `get-item` run **with the host
> role** (not your admin creds) returning no AccessDenied means it's live right now. Only if
> that probe is denied do you run the grant below.

```bash
ROLE=<host-role-name>; ACCOUNT=<account-id>
aws iam put-role-policy --role-name "$ROLE" --policy-name patch-315-tenant-secrets-read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"Patch315\",\"Effect\":\"Allow\",\"Action\":\"dynamodb:GetItem\",\"Resource\":\"arn:aws:dynamodb:$REGION:$ACCOUNT:table/openclaw-tenant-secrets\"}]}"
# verify (re-run Step 0e — should no longer be AccessDenied).
# NOTE: do NOT roll this back — it's a read-only, fail-closed prerequisite; harmless to keep,
# and removing it can break the rolled-back code paths that read this table.
```

---

## Step 3 — Update the S3 scripts source (BEFORE the Lambda step)

Hosts pull `launch-vm.sh` / `host-agent.py` from S3 at boot (`init-host.sh`). Update S3 **before**
the Lambda step so that any host that boots (ASG replacement / health-recovery / manual
scale-out) during the rollout gets the new #315 host code — not old code paired with a new
Lambda that's already allowing higher concurrency.

> **Do NOT chmod +x these files, and do not require it.** S3 objects carry no unix permission
> bit — a `+x` you set before `aws s3 cp` is NOT stored in S3 and NOT propagated to the next
> host; the downloaded file's mode comes from the host's umask (0644). The code never needs it:
> it runs them as `bash launch-vm.sh` / `python3 host-agent.py` (explicit interpreters, which
> ignore the execute bit). A `./launch-vm.sh`-style direct exec is never used. So mode 0644 is
> correct everywhere; "must be +x or it won't run on Linux" is a myth here and, worse, would be
> a permission that can't survive the S3 round-trip anyway.

> **Hashes in `manifest.json` are SHA-256** — verify with `shasum -a 256 <file>`. If your review
> tool prints a shorter/different hash it's almost certainly SHA-1 (the common default); that is
> a **false mismatch**, not a bad artifact. E.g. `launch-vm.sh.patched` is SHA-256 `f73a601c…`
> (matches manifest) but SHA-1 `edd7e6e2…`; `host-agent.py.patched` is SHA-256 `a7daa3ce…`.
> Always compare the SHA-256, not whatever a tool defaults to.

```bash
# optional pre-upload integrity check against manifest (SHA-256):
shasum -a 256 host-scripts/launch-vm.sh.patched host-scripts/host-agent.py.patched
#   expect launch-vm.sh.patched=f73a601c…  host-agent.py.patched=a7daa3ce…

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
half-written file. **No `chmod +x` needed:** launch-vm.sh is invoked as `bash launch-vm.sh` and
host-agent.py as `python3 host-agent.py` (explicit interpreters), so the execute bit is
irrelevant — mode 0644 is correct.

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

> **⚠️ The `KillMode=process` drop-in only covers the hosts you touch here — NEW hosts are born
> unsafe.** The drop-in lives in `/etc/systemd/system/host-agent.service.d/` on each existing
> host; it is NOT baked into the Launch Template. So every host the ASG launches later
> (scale-out, health-replacement, instance-refresh) comes up with the DEFAULT
> `KillMode=control-group` again — a latent trap: fine at steady state, but a future
> `systemctl restart host-agent` on that host would SIGTERM the whole cgroup and kill every
> tenant VM on it. This is a real gap confirmed on a freshly-launched host. Two fixes:
>
> - **Stopgap:** apply the same drop-in to each new host as it appears (doesn't scale — the ASG
>   keeps making new ones).
> - **Durable:** bake `KillMode=process` into `host-agent.service` in the Launch Template
>   (Step 6's LT-rebuild path — decode the current UserData, add the line to the unit, re-bake),
>   so new hosts are born safe. The proper long-term fix is in the source `host-agent.service`
>   (upstream `deploy/userdata/host-agent.service` has no `KillMode` today) — track it there.

---

## Step 5 — Update the three Lambda functions (update-function-code only)

Two shapes shipped:

- **`lambda/api/`** — source TREE (36 files), NOT a prebuilt zip. Its deps (`cryptography` etc.)
  are arm64 native wheels, so the zip must be built for the Lambda arm64 runtime — you build it
  at apply time (on an arm64 host, or with `--platform manylinux2014_aarch64`), OR reuse the
  deps already inside the live package (the "overlay" approach — verified; see the box below).
- **`lambda/scaler/openclaw-scaler.zip`** — PREBUILT zip (scaler is pure source, no third-party
  deps), so you upload it directly, no build step.

- **openclaw-api**: API Gateway calls it through an **alias** (commonly named `live`, but
  **do NOT assume** — discover it, some deployments use a different alias or none). The dispatch
  SQS ESM is bound to its **`$LATEST`**. So the moment you `update-function-code`, the dispatch
  path runs the new #315 code (before/independent of the alias). Update code → publish → move
  whatever alias fronts the API.
- **openclaw-lifecycle-consumer** (`$LATEST`): this customer has `create_via_queue=false`, so
  **create does NOT go through the consumer** — it handles start/stop/restart/rebuild/delete.
  It must still be updated (same `api/` source) for the #303/#304/#305 rebuild + #263 delete
  semantics. **Verify it via a rebuild/delete, not via create.**
- **openclaw-scaler** (`$LATEST`): upload the prebuilt `openclaw-scaler.zip` directly.

> **Why the api ships as a source tree, not a prebuilt zip:** its deps (`cryptography`) are
> arm64 native wheels. A zip we prebuild would freeze OUR deps versions onto your function —
> a change you didn't ask for. #315 changed only `.py`, not `requirements.txt`, so the RIGHT
> move is to **reuse the deps already in your live package** and overlay only the new source.
> That keeps YOUR dep versions untouched and needs no build host. This is the primary recipe
> below (verified end-to-end on an arm64/ddb env). A `pip`-build fallback follows if you'd
> rather rebuild deps (e.g. your live package is somehow missing them).

**Primary — overlay (reuse the live package's deps; no build host, no dep change):**

```bash
set -euo pipefail
LAMBDA_DIR="$(cd "$(dirname "$0")" && pwd)"; REGION=<region>
BK=~/Downloads/315-concurrent-dispatch-rollup/backup

# Discover the alias fronting openclaw-api (do NOT hardcode 'live'). Empty => API GW may call
# $LATEST directly; then there's no alias to move and rollback is purely $LATEST re-deploy.
API_ALIAS=$(aws lambda list-aliases --function-name openclaw-api --region $REGION \
  --query 'Aliases[0].Name' --output text 2>/dev/null); [ "$API_ALIAS" = "None" ] && API_ALIAS=""
echo "openclaw-api alias='${API_ALIAS:-<none>}'; pre-patch anchor versions are in $BK/lambda/anchors.txt (durable rollback)"

# 1. download the CURRENT api package (it already contains your platform-correct deps)
rm -rf /tmp/api-overlay && mkdir -p /tmp/api-overlay
URL=$(aws lambda get-function --function-name openclaw-api --region $REGION --query 'Code.Location' --output text)
curl -fsSL "$URL" -o /tmp/api-cur.zip && ( cd /tmp/api-overlay && unzip -q /tmp/api-cur.zip )
# 2. delete ONLY the first-party source dirs (keep cryptography/ jwt/ aws_lambda_powertools/ + the .so)
( cd /tmp/api-overlay && rm -rf consumers core routes services handler.py requirements.txt test_*.py __pycache__ )
# 3. overlay the shipped patched source tree
cp -a "$LAMBDA_DIR/api/." /tmp/api-overlay/
# 4. re-zip; sanity-check both the #315 code AND the reused deps are present.
#    NOTE: capture the listing into a var first, THEN grep it. Piping `unzip -l big.zip | grep -q`
#    lets grep close the pipe on first match, SIGPIPE-killing unzip (exit 141) — under
#    `set -o pipefail` that would mark the whole line failed and STOP even though the match
#    succeeded (a false negative). Grepping a variable avoids the pipe entirely.
( cd /tmp/api-overlay && zip -qr /tmp/api-lambda.zip . )
ZIPLIST=$(unzip -l /tmp/api-lambda.zip)
printf '%s\n' "$ZIPLIST" | grep -qE 'core/dispatch/binpack\.py' && printf '%s\n' "$ZIPLIST" | grep -qi 'cryptography/' \
  || { echo "STOP: overlay zip missing #315 code or deps"; exit 1; }

# openclaw-api: update $LATEST → wait → INVOKE-VERIFY $LATEST → only then publish + move alias.
# The verify gate matters: update-function-code makes the dispatch ($LATEST) path run the new
# code immediately, but the API-GW alias still points at the OLD version. So invoke $LATEST here
# and confirm it cold-starts; if the overlay zip is broken (bad import), STOP and roll $LATEST
# back from backup — API-GW users are untouched because the alias never moved.
aws lambda update-function-code --function-name openclaw-api --zip-file fileb:///tmp/api-lambda.zip --region $REGION >/dev/null
aws lambda wait function-updated --function-name openclaw-api --region $REGION
# invoke $LATEST with a read-only synthetic event. This gate ONLY proves the new code LOADS
# (no import/dependency breakage) — judge it by FunctionError=None, NOT by the response body.
# On a private API a synthetic {"resource":"/ping"} legitimately returns a 404 body: #298 makes
# the handler route by event["path"] via /{proxy+}, and this synthetic event isn't a real proxy
# event, so a 404 here is EXPECTED and fine. A broken overlay shows up as FunctionError!=None
# (e.g. Unhandled/import error), which is what we trap. Real route behavior is validated in Step 7.
printf '{"resource":"/ping","path":"/ping","httpMethod":"GET","headers":{},"requestContext":{"resourcePath":"/ping","httpMethod":"GET"}}' > /tmp/ping.json
FERR=$(aws lambda invoke --function-name openclaw-api --qualifier '$LATEST' \
  --payload fileb:///tmp/ping.json /tmp/ping.out --region $REGION --query FunctionError --output text)
[ "$FERR" = "None" ] || { echo "STOP: \$LATEST invoke has FunctionError=$FERR — overlay likely broken; roll \$LATEST back from backup, alias untouched"; exit 1; }
grep -qiE 'Unable to import|ModuleNotFound|ImportError' /tmp/ping.out && { echo "STOP: import error in new code — roll \$LATEST back"; exit 1; } || echo "\$LATEST invoke OK (body may be 404 — that's expected; FunctionError=None is the pass) — safe to publish + move alias"
# verified → publish and move the alias (if any)
NEW_VER=$(aws lambda publish-version --function-name openclaw-api --region $REGION --query Version --output text)
[ -n "$API_ALIAS" ] && aws lambda update-alias --function-name openclaw-api --name "$API_ALIAS" --function-version "$NEW_VER" --region $REGION >/dev/null
```

<details><summary><b>Fallback — rebuild deps with pip (only if the live package lacks them; needs arm64/manylinux)</b></summary>

```bash
rm -rf /tmp/api-build && mkdir -p /tmp/api-build
pip install --no-cache-dir --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --upgrade -r "$LAMBDA_DIR/api/requirements.txt" -t /tmp/api-build
cp -a "$LAMBDA_DIR/api/." /tmp/api-build/
( cd /tmp/api-build && zip -qr /tmp/api-lambda.zip . )
ZIPLIST=$(unzip -l /tmp/api-lambda.zip)   # capture then grep (avoid SIGPIPE false-fail under pipefail)
printf '%s\n' "$ZIPLIST" | grep -qi aws_lambda_powertools || { echo "STOP: deps not bundled"; exit 1; }
# then the same update-function-code / publish / update-alias as above.
```

</details>

# consumer: same api zip, $LATEST

aws lambda update-function-code --function-name openclaw-lifecycle-consumer --zip-file fileb:///tmp/api-lambda.zip --region $REGION >/dev/null
aws lambda wait function-updated --function-name openclaw-lifecycle-consumer --region $REGION

# scaler: upload the PREBUILT zip directly (pure source, no deps — no build step)

aws lambda update-function-code --function-name openclaw-scaler --zip-file "fileb://$LAMBDA_DIR/scaler/openclaw-scaler.zip" --region $REGION >/dev/null
aws lambda wait function-updated --function-name openclaw-scaler --region $REGION

# confirm all THREE CodeSha256 changed vs backup (a missed one = fix half-applied)

for FN in openclaw-api openclaw-lifecycle-consumer openclaw-scaler; do
NOW=$(aws lambda get-function --function-name $FN --region $REGION --query 'Configuration.CodeSha256' --output text)
  WAS=$(python3 -c "import json;print(json.load(open('$HOME/Downloads/315-concurrent-dispatch-rollup/backup/lambda/$FN.get-function.json'))['Configuration']['CodeSha256'])")
[ "$NOW" != "$WAS" ] && echo "$FN updated OK" || { echo "STOP: $FN CodeSha256 unchanged — not updated"; exit 1; }
done

````

**Rollback:** see the dedicated "Rollback the control-plane Lambdas" section below —
openclaw-api needs BOTH its alias path and its `$LATEST`/dispatch path reverted.

---

## Step 6 — Future machines (only if #300 probe hit; NO cdk deploy)

`init-host.sh` is baked into LT `openclaw-host-lt` UserData, not pulled from S3. Running hosts
already booted — this only matters for future scale-out, and only if Step 0d showed #300.

> **Two traps here — read before touching the LT:**
>
> 1. **The shipped `init-host.sh.patched` is a TEMPLATE, not a ready file.** It still contains
>    ~32 `{{PLACEHOLDER}}` tokens (`{{SUBNET_PREFIX}}`, `{{AVAIL_VCPU}}`, `{{AMP_REMOTE_WRITE_URL}}`,
>    …) that CDK substitutes at synth time before baking into UserData. **Do NOT base64/gzip the
>    shipped template into a new LT version as-is** — a host booting it would get literal `{{...}}`
>    and fail. Instead, work like the Lambda overlay: take your CURRENT LT version's already-
>    rendered UserData (decode it), apply ONLY the #300 change to that rendered init-host, and
>    re-bake. Reuse your rendered values; don't re-render.
> 2. **Find the version the ASG actually uses — NOT `$Default`.** LT default and latest commonly
>    diverge (e.g. Default=2 but the ASG pins v4, hand-modified). Decode the version the ASG
>    references, record it as your rollback anchor, and diff/patch against THAT.

```bash
REGION=<region>; LT=openclaw-host-lt; ASG=<asg-name>
# 1. which LT version does the ASG actually use? (rollback anchor — do NOT assume $Default)
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" --region $REGION \
  --query 'AutoScalingGroups[0].[LaunchTemplate,MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification]' --output json
# 2. decode THAT version's UserData (already-rendered init-host, no {{...}}):
CUR_VER=<the version number from step 1>
aws ec2 describe-launch-template-versions --launch-template-name "$LT" --versions "$CUR_VER" --region $REGION \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d > /tmp/ud.cur
#    (if UserData is a bootstrap that gunzips an inner blob, that inner blob is the real init-host.sh)
# 3. apply ONLY the #300 fix to the RENDERED init-host (diff the shipped template vs your rendered
#    file to isolate the #300 hunk, then apply just that hunk — keep all your rendered values).
# 4. re-bake as a NEW LT version and point the ASG at it (existing hosts untouched; NO instance
#    refresh unless you explicitly choose to). Keep $CUR_VER as the rollback anchor.
```

**Validate the new LT by booting ONE new host — never trust the re-bake blind.** A bad re-bake
(unreplaced `{{...}}`, gzip/base64 wrong) only shows up at boot, and it must not reach the fleet.
So: point the ASG at the new version but do a controlled single-host launch and watch it come up
clean, BEFORE it can affect anything. Existing hosts are never touched (no instance refresh).

```bash
REGION=<region>; ASG=<asg-name>; LT=openclaw-host-lt
# 0. sanity BEFORE boot: the re-baked UserData must NOT contain literal placeholders.
aws ec2 describe-launch-template-versions --launch-template-name "$LT" --versions '$Latest' --region $REGION \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d > /tmp/ud.new
grep -qE '\{\{[A-Z_]+\}\}' /tmp/ud.new && { echo "STOP: new UserData still has {{placeholders}} — re-bake wrong, do NOT roll out"; } || echo "no literal placeholders — ok to boot one"
#   (if UserData is a bootstrap that gunzips an inner blob, decode that inner blob and grep it instead)

# 1. launch ONE new host on the new LT (temporary +1; do NOT instance-refresh existing hosts).
#    e.g. bump desired by 1 and confirm ASG uses the new LT version, or launch a standalone
#    instance from the new LT version into the host subnet.
# 2. watch that ONE host boot clean — three signals (fails => the re-bake is bad):
#    a) init log has no placeholder/interpreter error:
ssh_or_ssm <new-host> 'grep -nE "\{\{|command not found|FATAL" /var/log/openclaw-init.log | tail'   # expect empty
#    b) it registered into the hosts table (init-host step5 registers on success):
aws dynamodb get-item --table-name openclaw-hosts --key '{"host_id":{"S":"<new-instance-id>"}}' --region $REGION --query 'Item.status.S'
#    c) the ASG lifecycle hook got CONTINUE (not Heartbeat Timeout -> ABANDON):
aws autoscaling describe-scaling-activities --auto-scaling-group-name "$ASG" --region $REGION --max-items 5 \
  --query 'Activities[].{S:StatusCode,D:Description}' --output table
# 3. GREEN (registered + no init error + lifecycle CONTINUE) -> the new LT is good; leave it.
#    RED -> terminate that one host, roll the ASG back to $CUR_VER (the anchor). Existing hosts
#    never saw the new LT, so the fleet is unaffected either way.
```

This whole step is **optional for this customer** (skip unless #300 is confirmed in Step 0d).
Running hosts already booted past init-host, so it only affects future scale-out.

---

## Step 6.5 — Secrets Manager VPCE (carried from #309; probe first, describe-only, human-gated)

**Not a #315 change** — carried forward for a 266-only customer. **AI is describe-only on
network resources: run only the probes, then STOP and ask a human before any create.** A wrong
VPCE/DNS change can break resolution for the whole VPC.

**When it's needed:** only if the observability/AOS stack (OpenSearch + the in-VPC roles-mapping
Lambda) is deployed AND that Lambda can't reach Secrets Manager (no NAT). If `logging.enabled=false`
(this customer), AOS isn't deployed → **this whole layer is a no-op, skip it.**

```bash
REGION=<region>
# Probe with TWO signals (a renamed function could slip a single name match):
aws opensearch list-domain-names --region $REGION --output text        # (a) any OpenSearch domain?
aws lambda list-functions --region $REGION \
  --query "Functions[?contains(FunctionName,'oles')||contains(FunctionName,'aos')||contains(FunctionName,'AOS')].FunctionName" --output text   # (b) any roles-mapping Lambda?
#   BOTH empty  => observability not deployed => VPCE not needed => SKIP this whole step.
#   Either non-empty => an AOS component may exist. Do NOT auto-create. Identify the in-VPC
#     roles-mapping Lambda with the operator; if it already reaches Secrets Manager (NAT default
#     route on its subnets, or an existing Interface VPCE with private DNS), nothing to do.
#   Only if it genuinely lacks egress: PROPOSE (do not run) this, and STOP for human approval:
#     aws ec2 create-security-group --group-name openclaw-sm-vpce-315 --vpc-id <vpc> --region $REGION ...
#     aws ec2 authorize-security-group-ingress --group-id <sg> --protocol tcp --port 443 --source-group <lambda-sg> ...
#     aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --service-name com.amazonaws.$REGION.secretsmanager \
#       --vpc-id <vpc> --subnet-ids <lambda-subnets> --security-group-ids <sg> --private-dns-enabled --region $REGION
# rollback (only if you created it): aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <id>; delete-security-group --group-id <sg>
````

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
#       PASS = FunctionError=null + no 'Unable to import' in logs. Do NOT require a 200 body:
#       on a private API this synthetic /ping returns a 404 body (it's not a real /{proxy+}
#       event — #298 routes by path), which is EXPECTED and not a failure. FunctionError=null
#       is the signal that the repackaged deps load. Real route behavior = Step 7b.
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

## Rollback the host / S3 / LT layers

- **host scripts (both files, every host)** — restore the `.bak.315` copies and restart the
  agent. host-agent.py is run by systemd as `python3 /opt/openclaw/host-agent.py` and
  launch-vm.sh as `bash /home/ubuntu/launch-vm.sh`, so **neither needs an execute bit** — a
  plain `cp` (mode 0644) is fine on both restore and apply.

  ```bash
  for h in <host1> <host2>; do
    ssh -i <key> ubuntu@$h '
      sudo cp /opt/openclaw/host-agent.py.bak.315 /opt/openclaw/host-agent.py
      cp /home/ubuntu/launch-vm.sh.bak.315 /home/ubuntu/launch-vm.sh
      sudo systemctl restart host-agent; sleep 3; systemctl is-active host-agent'
  done
  # the KillMode=process drop-in can stay — it's a correctness fix, harmless to keep.
  ```

- **S3 scripts** — also roll back, or a future host will re-pull the new code at boot:
  `for f in launch-vm.sh host-agent.py; do aws s3 cp "$BK/s3/$f" "$BASE/$f" --region $REGION; done`
- **LT** (only if you added a version in Step 6): point the ASG back at the prior version number.
- **IAM — do NOT roll back.** The Step-2 grant is a read-only permission
  (`dynamodb:GetItem` on `openclaw-tenant-secrets`) and a fail-closed prerequisite of the token
  fallback. It is harmless to keep, and removing it can break the (rolled-back) code paths that
  rely on it. Leave it in place.
