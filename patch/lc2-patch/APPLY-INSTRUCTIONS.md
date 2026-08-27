# fix-0826 — apply by reading files, no CloudFormation stack update

`manifest.json` is the single source of truth. When this document and the manifest disagree
about a hash, a path or a logical id, the manifest wins.

- range: gateway `c9fd494ff4a76929f205f52464047a9185c7c49a` → `10e38efc18073279a9e74eee4ce371e3dfa08edd`
- the base is the `patch_sha` of `patch/lifecycle-op-patch`, i.e. **the revision this
  environment is actually on**. 228 paths, 22 fixes, 29 verifications.
- `status: MANUAL_REVIEW`. Every operation that owns a synthesized CloudFormation resource is
  `MANUAL_CLI_REVIEW` by design — read why in step 4 before you approve anything.
- No step here updates a CloudFormation stack.

## What this kit replaces

`patch/edge-balancer-cosocket-606` was **skipped in this environment, and that was the right
call**: it shipped `route.lua` and `nginx.conf` but not `balancer.lua`, `backend.lua` or
`redis_client.lua`, while the `route.lua` it shipped calls into all three. Applying it alone
produces this, measured on a real edge host on 2026-08-26 with exactly that file combination:

| host state | `/healthz` | a real `/ws/<tenant>` request |
| --- | --- | --- |
| that kit applied alone | **200** | **connection reset, no HTTP status** (`curl` reports `000`) |
| complete set installed | 200 | 404 for an unknown tenant — the normal answer |

Both the rewrite phase (`route.lua:181`, `consume_retry_hint`) and the output filter
(`route.lua:234`, `fixup_status`) abort, so nginx cannot emit response headers at all. `/healthz`
answers 200 in **both** states because it is a separate `location` from `location ~ ^/ws/[^/]+`,
so the load balancer's target group and the scaling group never see the outage — and `/healthz`
can never tell you whether the fix worked.

**This kit ships the complete set**, so that trap is closed here: `#606` covers `balancer.lua`,
`backend.lua`, `route.lua`, `nginx.conf` and the three affected specs, and `redis_client.lua`
travels with them because `backend.lua` computes `LOCK_TIMEOUT_SEC` from
`redis_client.MAX_SEQUENTIAL_READS` **at module load time** — a newer `backend.lua` beside an
older `redis_client.lua` fails to load the module. There is no separate prerequisite to run.

## Step 0 — Discover the environment, then prove the kit is authentic

```bash
bash lib/discover-env.sh > environment.json      # READ-ONLY. Everything downstream reads this.
export OC_RUN_ID="fix0826-$(date -u +%Y%m%d-%H%M%S)"   # rollback restores THIS run's state
export OC_RECEIPT_FILE="cfn-verify-receipt.$OC_RUN_ID.txt"
```

`OC_RUN_ID` is mandatory: every operation writes its pre-change state under that id, and
`rollback` refuses to read a file from a different run. Without it a rollback could restore a
stale definition from an earlier attempt.

It must name: region, account, the control-plane API the deployed client actually calls (proved
with that client's own auth, not inferred from a route shape), the Lambda alias the API invokes
**and** the `$LATEST` the dispatch event-source mapping binds, the edge ASG correlated to the
hosts ledger, the Launch Template version that ASG actually pins, and the host instance ids. If
any is `null`, stop — a later step would otherwise act on a guess.

This range also needs the **host** ASG, because `init-host.sh` changed (Step 3b). Take it from the
hosts ledger the same way the edge ASG is taken — a host ASG has hosts registered in
`openclaw-hosts` and an edge ASG does not, so the two are told apart by that, never by a name
pattern:

```bash
export HOST_ASG=...       # the host ASG; Step 3b reads the LT and version IT pins, not $Default
export ASSETS_BUCKET=...  # the assets bucket, shared with the S3 operations
```

Then prove every artifact equals the source it claims:

```bash
set -o pipefail
python3 - <<'EOF'
import hashlib, json, pathlib
m = json.load(open("manifest.json"))
checked, bad = 0, []
for p, v in m["paths"].items():
    a = v.get("artifact")
    if not a:
        continue
    checked += 1
    if hashlib.sha256(pathlib.Path(a).read_bytes()).hexdigest() != v["patch_sha256"]:
        bad.append(p)
print("artifacts checked:", checked, "mismatched:", bad or "none")
raise SystemExit(1 if bad or checked == 0 else 0)
EOF
```

Hashes are **SHA-256**; a tool defaulting to SHA-1 shows a false mismatch. A non-empty mismatch,
or a checked count of zero, means the kit is mis-packaged — do not apply it.

## Step 1 — Back up, per host and per function

Fleet drift is expected; hosts legitimately run different versions. Back up **per host** so each
rolls back to its own version. This kit converges them.

```bash
set -o pipefail
# 1. anchor the current version
aws lambda publish-version --function-name "$OPENCLAW_API_FN" \
  --description "pre-fix-0826 anchor" --query Version --output text
aws lambda get-alias --function-name "$OPENCLAW_API_FN" --name "$OPENCLAW_API_ALIAS" \
  --query FunctionVersion --output text                     # note it; the tool also records it
# 2. the code half of the rollback needs a real object. lambda-api-code apply REFUSES to run
#    until this exists, because otherwise its rollback cannot restore anything.
url=$(aws lambda get-function --function-name "$OPENCLAW_API_FN" \
        --query 'Code.Location' --output text)
curl -sS "$url" -o "/tmp/openclaw-api-pre-$OC_RUN_ID.zip"
export BACKUP_S3_BUCKET="$ASSETS_BUCKET"
export BACKUP_S3_KEY="rollback/openclaw-api-pre-$OC_RUN_ID.zip"
aws s3 cp "/tmp/openclaw-api-pre-$OC_RUN_ID.zip" "s3://$BACKUP_S3_BUCKET/$BACKUP_S3_KEY"
aws s3api head-object --bucket "$BACKUP_S3_BUCKET" --key "$BACKUP_S3_KEY" >/dev/null \
  && echo "rollback artifact in place"
```

On every host copy what this kit replaces: `host-agent.py`, `stop-vm.sh`, `launch-vm.sh`,
`lib/harden-config.sh`. On every edge host copy `route.lua`, `nginx.conf`, `lib/balancer.lua`,
`lib/backend.lua`, `lib/redis_client.lua` and `install-edge.sh`, and write a `SHA256SUMS`
alongside them.

SSH is commonly blocked (private subnets are the norm when `api.mode=private`). Commands read as
`ssh`/`scp`; the transport is SSM. A shell command becomes `aws ssm send-command`; a file push
becomes a base64 payload decoded on the host — the SSM `commands` array loses newlines and the
shell is `dash`, so never embed a multi-line script directly.

## Step 2 — Fix the running machines

Order is enforced by the tooling, not just by this document: `s3-edge-bundle apply` refuses to
run until the EdgeRole grant's readback says `allowed`. So the grant comes first.

**2a. The fail-closed prerequisite, before any edge change:**

```bash
export AWS_REGION=... EDGE_ROLE_ARN=... EDGE_ROLE_NAME=...
bash lib/apply-resource-ops.sh iam-edge-putmetricdata apply  resources/cloudformation "$AWS_REGION"
bash lib/apply-resource-ops.sh iam-edge-putmetricdata verify  resources/cloudformation "$AWS_REGION"
```

`apply` skips adding a duplicate if an equivalent grant already evaluates to `allowed`, asserts
the effective decision by reading it back, and only then writes its receipt line. Its `rollback`
is a deliberate no-op that exits 0 with an explanation — removing this grant re-blinds fleet
convergence.

**2b. Host scripts (layer B-s3).** Mode is **0644**, not 0755 — these run as
`bash stop-vm.sh` / `python3 host-agent.py`, and they travel through S3, which carries no unix
permission bit, so a `+x` set before upload is neither stored nor propagated.

| artifact | target |
| --- | --- |
| `host-scripts/host-agent.py.patched` | `/home/ubuntu/host-agent.py` |
| `host-scripts/stop-vm.sh.patched` | `/home/ubuntu/stop-vm.sh` |
| `host-scripts/launch-vm.sh.patched` | `/home/ubuntu/launch-vm.sh` |
| `host-scripts/lib/harden-config.sh.patched` | `/home/ubuntu/lib/harden-config.sh` |

Copying the file is not applying it: `host-agent.py` runs as a long-lived service, so the old
code stays resident until it restarts. `stop-vm.sh`, `launch-vm.sh` and `harden-config.sh` are
re-read per invocation and need no restart. Per host:

```bash
set -o pipefail
for f in host-agent.py stop-vm.sh launch-vm.sh lib/harden-config.sh; do
  sha256sum "/home/ubuntu/$f"          # compare each against the manifest patch_sha256
done
systemctl restart host-agent            # or the unit name this host actually uses
systemctl is-active host-agent          # must print active
# prove the RUNNING process is the new code, not just that the file on disk changed:
pid=$(systemctl show -p MainPID --value host-agent)
sha256sum "/proc/$pid/cwd/host-agent.py" 2>/dev/null || \
  ls -l "/proc/$pid/exe" "/proc/$pid/cmdline"
journalctl -u host-agent --since '-2 min' --no-pager | tail -20   # no import error, no crash loop
```

If `is-active` is not `active`, or the log shows a restart loop, roll that host back from its own
backup before touching the next one. Fleet drift is expected; converge one host at a time.

**2c. Edge modules — all five together.** The permission gate applies here too, not only to
the S3 bundle: run it before replacing anything on a running edge host, so a box cannot end up
running the metric-emitting installer without the grant.

```bash
bash lib/apply-resource-ops.sh iam-edge-putmetricdata gate resources/cloudformation "$AWS_REGION"
```

Then install as 0644, verify **every** file including `nginx.conf` *before* reloading, then
reload and export the mark the verifications consume:

```bash
set -o pipefail
LUALIB=/usr/local/openresty/lualib/edge
CONF=/usr/local/openresty/nginx/conf/nginx.conf
install -m 0644 host-scripts/edge/route.lua            "$LUALIB/route.lua"
install -m 0644 host-scripts/edge/lib/balancer.lua     "$LUALIB/lib/balancer.lua"
install -m 0644 host-scripts/edge/lib/backend.lua      "$LUALIB/lib/backend.lua"
install -m 0644 host-scripts/edge/lib/redis_client.lua "$LUALIB/lib/redis_client.lua"
install -m 0644 host-scripts/edge/nginx.conf           "$CONF"
for f in "$LUALIB/route.lua" "$LUALIB/lib/balancer.lua" "$LUALIB/lib/backend.lua" \
         "$LUALIB/lib/redis_client.lua" "$CONF"; do sha256sum "$f"; done
# all five hashes must equal the manifest patch_sha256 for their source paths before reloading
/usr/local/openresty/nginx/sbin/nginx -t -c "$CONF"
export OC_EDGE_RELOAD_MARK="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
systemctl reload claw-edge
echo "OC_EDGE_RELOAD_MARK=$OC_EDGE_RELOAD_MARK"   # the #639 check requires this
```

`nginx -t` parses the config but **does not load Lua modules**, so a green `-t` is necessary and
not sufficient. Judge with a business-port request and a log window bounded by the mark:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/ws/t-0000000000000000   # expect 404
journalctl -u claw-edge --since "$OC_EDGE_RELOAD_MARK" --no-pager \
  | grep -cE 'fixup_status|MAX_SEQUENTIAL_READS|nil value'                              # expect 0
```

404 means the chain is closed; `000` means it is not. Port 8080 is the business server —
**9145 is the metrics server** and probing it proves nothing.

**2d. Edge bundle for future instances** (running boxes already have it from 2c):

```bash
export ASSETS_BUCKET=...
bash lib/apply-resource-ops.sh s3-edge-bundle apply  resources/cloudformation "$AWS_REGION"
bash lib/apply-resource-ops.sh s3-edge-bundle verify resources/cloudformation "$AWS_REGION"
bash lib/apply-resource-ops.sh s3-obs-assets  apply  resources/cloudformation "$AWS_REGION"
```

**2c. Lambda (layer C-lambda) — overlay, do not prebuild a zip.** This function carries arm64
native wheels; a zip you build freezes your dependency versions onto this environment. Reuse the
live package and replace **only the individual files this kit ships**.

Do **not** delete `core/` or `services/` and overlay `lambda/api` on top. Measured against a live
package: `core/` holds 34 files and this kit ships 4 of them; `services/` holds 24 and the kit ships
4. Deleting either directory drops 50 files including `core/__init__.py` and `core/auth.py`, and the
function then fails at import — worse than the bug being patched.

Build it file-by-file, and prove the entry set did not change:

```bash
KIT=$PWD
python3 - "$KIT" /tmp/live.zip /tmp/openclaw-api-overlay.zip <<'PY'
import hashlib, sys, zipfile
from pathlib import Path
kit = Path(sys.argv[1]) / "lambda" / "api"
shipped = {str(f.relative_to(kit)): f for f in sorted(kit.rglob("*")) if f.is_file()}
zin = zipfile.ZipFile(sys.argv[2]); names = [i.filename for i in zin.infolist()]
absent = [r for r in shipped if r not in names]
if absent:
    sys.exit(f"FAIL: kit files not present in the live package: {absent}")
with zipfile.ZipFile(sys.argv[3], "w", zipfile.ZIP_DEFLATED) as zo:
    for i in zin.infolist():
        data = shipped[i.filename].read_bytes() if i.filename in shipped else zin.read(i)
        ni = zipfile.ZipInfo(i.filename, date_time=i.date_time)
        ni.external_attr, ni.compress_type = i.external_attr, zipfile.ZIP_DEFLATED
        zo.writestr(ni, data)
zc = zipfile.ZipFile(sys.argv[3]); got = [i.filename for i in zc.infolist()]
if got != names:
    sys.exit(f"FAIL: entry set changed {len(names)} -> {len(got)}")
differ = [n for n in names
          if hashlib.sha256(zin.read(n)).hexdigest() != hashlib.sha256(zc.read(n)).hexdigest()]
extra = sorted(set(differ) - set(shipped))
if extra:
    sys.exit(f"FAIL: entries differ that the kit does not ship: {extra[:8]}")
print(f"{len(got)} entries unchanged; {len(differ)} differ, all shipped by this kit")
PY
```

`/tmp/live.zip` is the package downloaded in APPLY step 1; confirm it hashes to the live
`CodeSha256` before using it, or you are patching a stale package.

Note which shipped files come out byte-identical to live — that means the change is already deployed
and is a fact worth recording, not a failure.

Then back the live package up to a **versioned** key and let the operation drive the change. It
publishes the version **last**, so the immutable version carries the code *and* the eight deadline
values; publishing with the code (`update-function-code --publish`) snapshots a version before the
environment is written, and the alias would then point at a version whose configuration omits them.

```bash
export OVERLAY_ZIP=/tmp/openclaw-api-overlay.zip
export BACKUP_S3_BUCKET=<a versioned bucket>  BACKUP_S3_KEY=patch-backups/$OC_RUN_ID/openclaw-api-live.zip
aws s3 cp /tmp/live.zip "s3://$BACKUP_S3_BUCKET/$BACKUP_S3_KEY" --region "$AWS_REGION"

bash lib/apply-resource-ops.sh lambda-api-code  apply  resources/cloudformation "$AWS_REGION"
bash lib/apply-resource-ops.sh lambda-api-code  verify resources/cloudformation "$AWS_REGION"
bash lib/apply-resource-ops.sh lambda-api-alias apply  resources/cloudformation "$AWS_REGION"
bash lib/apply-resource-ops.sh lambda-api-alias verify resources/cloudformation "$AWS_REGION"
```

The operation refuses without that backup, and it downloads and hashes the object to prove it holds
the code running *now* — a stale object at that key would otherwise overwrite the one recoverable
copy during an unwind. The bucket must have versioning enabled; the restore pins a version id,
because a key is mutable.

The invoke verdict is `FunctionError` = `None`, **not** a 200 body: on a private API a synthetic path
returns 404 by routing, which is expected.

Both paths move, and neither alone reverts both: the API Gateway invokes the alias while the dispatch
event-source mapping binds `$LATEST`. Rollback is `lambda-api-alias rollback` **then**
`lambda-api-code rollback`. The version that apply published stays behind — a Lambda version is
immutable and nothing else points at it, so it is inert, but a rollback does **not** return the
version list to its pre-apply length.

## Step 3 — Fix the future-machine source

Upload each replaced host script to a temporary key, verify it, promote it into
`deployment/scripts/`, and keep the previous S3 version id for rollback.

The **edge Launch Template** is `MANUAL_CLI_REVIEW`. A new LT version alone does not update the
running ASG: it pins a specific version and only new instances use a new one. `pull` is what
records the rollback anchor, so it must run first:

```bash
bash lib/apply-lt.sh pull     "$EDGE_ASG" "$AWS_REGION"
bash lib/apply-lt.sh push     "$EDGE_ASG" "$AWS_REGION"
bash lib/apply-lt.sh promote  "$EDGE_ASG" "$AWS_REGION"
bash lib/apply-lt.sh verify   "$EDGE_ASG" "$AWS_REGION"
bash lib/apply-lt.sh rollback "$EDGE_ASG" "$AWS_REGION"   # only if verify fails
```

The tool refuses a floating `$Latest`, guards against drift and reads the result back. The only
change here is the bundle prefix; the live edge UserData is already rendered, so no
`{{PLACEHOLDER}}` template is involved.

### Step 3b — The host bootstrap object and the host Launch Template

`init-host.sh` changed, and its delivery is not a file copy. `ha_edge.py` computes
`sha256(rendered init-host.sh)` and uses that digest AS the S3 prefix
(`deployment/bootstrap/host/<sha256>/init-host.sh`); the host user data downloads that exact key,
verifies the digest, and only then executes it. Two consequences decide how this step works:

* **The digest in the closure is not your expected value.** It was computed from
  `config.yml.example`, so it belongs to example host reservations, rootfs prefix and egress CIDRs.
  Installing that object would boot hosts configured for a different deployment. The expected digest
  can only be *computed* — from this environment's own currently served script plus this range's
  change.
* **The change therefore has to be replayed, not copied.** `lib/init-host.sh.diff` is a
  template-level diff (base template → patch template). The operation reads the digest the
  ASG-pinned LT actually requests, downloads that object, asserts the object's own sha256 equals
  the digest in its key *and* in the user data, replays the diff onto it (a context line containing
  `{{TOKEN}}` matches whatever value is rendered there; everything else must match literally),
  recomputes the digest, uploads to the NEW prefix, and only then cuts an LT version whose user data
  differs from the current one in exactly that one digest.

```bash
export HOST_ASG=...          # the host ASG; the tool reads the LT and version IT pins
export ASSETS_BUCKET=...     # same bucket as the other S3 operations

bash lib/apply-resource-ops.sh host-init-bootstrap apply    resources/cloudformation "$AWS_REGION" lib/init-host.sh.diff
bash lib/apply-resource-ops.sh host-init-bootstrap verify   resources/cloudformation "$AWS_REGION" lib/init-host.sh.diff
bash lib/apply-resource-ops.sh host-init-bootstrap rollback resources/cloudformation "$AWS_REGION"
```

It stops instead of guessing in every one of these cases: the ASG pins `$Latest`/`$Default` rather
than an immutable version; the served object's digest disagrees with its own key; the diff's
before-image matches the live script zero times or more than once; the diff would ADD a line
containing a placeholder (nothing in this path substitutes one); or the re-baked user data differs
from the current one in more than the digest.

**It ends at promote.** No instance refresh is issued: running hosts keep serving and only newly
launched hosts read the new script. Replacing the fleet stays a separate human decision. Rollback
moves the default version back — the old prefix is still addressable because the deployment is
`prune=False` / `retain_on_delete=True` — and it deliberately leaves the uploaded object in place,
because deleting it would break any host that already launched on the new version.

## Step 4 — Stack-source changes, one resource at a time

Read the whole closure first — this is the read-only pass:

```bash
bash lib/apply-cfn-resources.sh plan resources/cloudformation "$AWS_REGION"
```

That tool prints each changed resource's before/after and **stops**; it never mutates, and it
deliberately does not invent a call for an arbitrary resource type. Use it to understand the
closure, not to change anything.

The changes are then applied by `lib/apply-resource-ops.sh`, which carries the reviewed call for
the resource types **this** kit changes and refuses an unknown op id rather than deriving one.
Every `apply` follows the same shape:

**precondition gate → mutate → read the live resource back → assert the readback → append a
receipt line.** The receipt is produced *by* a passing readback, so `cfn-verify-receipt.txt` is
evidence rather than an assertion: it cannot be written for a resource that was not changed, and
each `verify_cli` re-reads the live resource anyway rather than trusting the file.

```bash
export OC_RECEIPT_FILE=cfn-verify-receipt.txt   # default; set it if you want a per-run receipt
for op in iam-edge-putmetricdata lambda-api-code lambda-api-alias ssm-deadline-params \
          s3-edge-bundle s3-obs-assets; do
  bash lib/apply-resource-ops.sh "$op" apply  resources/cloudformation "$AWS_REGION"
  bash lib/apply-resource-ops.sh "$op" verify resources/cloudformation "$AWS_REGION"
done
```

`lambda-api-code apply` needs `OVERLAY_ZIP` plus the backup object from step 1, and it does
more than replace code. The closure's only change to this function is `Code.S3Key`, but the eight
`LIFECYCLE_DEADLINE_SEC_*` environment values are injected by a stack update, which this kit
deliberately never performs — so a live function whose config omitted a tier would keep raising at
import even after the new code lands. The operation therefore reads those eight values **out of
the closure** and merges them into the live environment, keeping every other key, then asserts:
`CodeSha256` equals the overlay zip's own digest, each of the eight values equals the closure
value individually, and a direct invoke returns `FunctionError=None`. If any assertion fails it
restores both the code and the previous environment before exiting non-zero.

`ssm-deadline-params` does the same for the eight `/openclaw/lifecycle/deadline-sec/*`
parameters, for the same reason. Those parameters are identical between the two commits, so the
operation claims no CloudFormation resource — it converges a running environment, and its
rollback puts back exactly the values it recorded (deleting the ones that did not exist).

`lambda-api-alias verify` compares the alias against the version `lambda-api-code` published and
checks the **qualified** version's `CodeSha256`. It never reads the unqualified `Version`, which
is `$LATEST` and would pass while the alias still served old code. Rollback needs both halves:
the alias, and `$LATEST` via `lambda-api-code rollback`, because the dispatch event-source
mapping binds `$LATEST`.

The two remaining operations need a decision, so run them separately and read their gates:

**`REPO_SOURCE_ZIP` is a repository archive you build**, not something the kit ships. The operation
asserts three markers (`setup.sh`, `build-rootfs.sh`, `deploy/app.py`) and requires every file the kit
ships under `host-scripts/deploy-machine/` to be present byte-for-byte, so build it from your checkout
of this commit with the kit's patched files laid over it:

```bash
python3 - . "$REPO_SOURCE_ZIP" <<'PY'
import sys, zipfile
from pathlib import Path
kit = Path(sys.argv[1]); out = sys.argv[2]
patched = {str(f.relative_to(kit / "host-scripts" / "deploy-machine")): f
           for f in sorted((kit / "host-scripts" / "deploy-machine").rglob("*")) if f.is_file()}
repo = Path.cwd()          # your checkout of this commit
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for rel, src in patched.items():
        z.writestr(rel, src.read_bytes())
    for f in sorted(repo.rglob("*")):
        rel = str(f.relative_to(repo))
        if f.is_file() and rel not in patched and not rel.startswith((".git/", "patch/")):
            z.writestr(rel, f.read_bytes())
print("built", out)
PY
```

Resolve the build role through CloudFormation, never by name: an account with stacks in several
regions holds several roles with the same logical id, and the first name match may be another
region's.

```bash
export GOLDEN_IMAGE_ROLE_NAME=$(aws cloudformation describe-stack-resource \
  --stack-name OpenClawImage --logical-resource-id GoldenImageBuildRole8D4C1F76 \
  --region "$AWS_REGION" --query 'StackResourceDetail.PhysicalResourceId' --output text)
export GOLDEN_IMAGE_ROLE_ARN="arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/$GOLDEN_IMAGE_ROLE_NAME"

# only where this environment rebuilds the guest rootfs from this repository.
# needs GOLDEN_IMAGE_PROJECT, GOLDEN_IMAGE_ROLE_NAME, GOLDEN_IMAGE_ROLE_ARN, CDK_ASSETS_BUCKET,
# REPO_SOURCE_ZIP. After repointing the project it asserts the build role can read the NEW key and
# unwinds ALL THREE changes (upload, role grant, project source) if it cannot — otherwise the next
# build fails on a permission it never had.
#
# The closure's asset KEY is authoritative for the new grant, but the OLD one is taken from the live
# policy: a CDK asset key is the content hash of the synth that produced it, so a real environment
# holds a third key that matches neither side of the closure. The bucket likewise comes from your
# account and is asserted against it.
bash lib/apply-resource-ops.sh codebuild-golden-image apply resources/cloudformation "$AWS_REGION"

# DELETES two live alarms. Needs REDIS_REPLICATION_GROUP_ID and EDGE_ASG. The parameter the edge
# reads for its Redis coordinate comes from the closure, not from your environment.
#
# The gate has three stages, all fail-closed. First the premise: the parameter the edge actually
# reads must resolve to this replication group's PRIMARY endpoint — that is what the closure declares
# and it is the only reason removing replica-lag alarms is safe. Then it enumerates the replication
# group. Then it takes the InService edge set from the ASG itself and asks EVERY one of those
# instances over SSM whether its configured reader host equals its primary. It aborts if the
# coordinate still resolves to the reader, on an empty InService set, on a supplied instance list
# that does not match the ASG, or on any box still pointed at a replica.
#
# `verify` checks the same premise. "Both alarms are absent" alone is not this operation's applied
# state: alarms that were never created look identical to alarms this kit deleted.
bash lib/apply-resource-ops.sh cw-drop-replication-lag-alarms apply resources/cloudformation "$AWS_REGION"
```

The two alarms are selected by their exact names from the closure —
`openclaw-edge-replica-route-freshness-upper-bound-1` and `-2` — never by a name prefix. A prefix
that matches nothing would let a destructive delete report success while changing nothing.

The closure was synthesized from both pinned commits with `edge.enabled=true`,
`redis.enabled=true`, and the observability provenance values pinned so the diff carries no
build-timestamp noise. Fifteen resources changed:

| resource | class | rollback |
| --- | --- | --- |
| `EdgeRoleDefaultPolicy` (IAM::Policy) — `cloudwatch:PutMetricData`, namespace-restricted | MANUAL_CLI_REVIEW | **RETAIN** |
| `ApiHandler` function + published version (A/D pair) | MANUAL_CLI_REVIEW | REDEPLOY_ZIP |
| `ApiHandlerLive` alias | MANUAL_CLI_REVIEW | ALIAS_FLIP |
| `Assets` tag + `EdgeBundleAssetDeployment` (new bundle prefix) | MANUAL_CLI_REVIEW | RESTORE |
| `ObsFbInstaller` / `ObsFbEdgeConf` / `ObsFbHostConf` deployments | MANUAL_CLI_REVIEW | RESTORE |
| `EdgeLaunchTemplate` | MANUAL_CLI_REVIEW | LT_REVERT |
| `GoldenImageBuildRole` policy + `GoldenImageBuilder` | MANUAL_CLI_REVIEW | RESTORE |
| **`RedisReplicationLagReplica1Alarm` + `Replica2Alarm` — DELETION of two live alarms** | MANUAL_CLI_REVIEW | RESTORE |

Three of these need a decision, not just an approval:

**The two ReplicationLag alarm deletions are a LOSS of observability.** They exist here today
because the previous revision derived their node ids from the replication group's read endpoints.
The new revision creates them only when `redis.edge_read_from_replica` is true **and** at least
one replica exists — with the switch off, the reader parameter equals the primary and a lag alarm
on it is meaningless.

The gate is executable, not advisory: `cw-drop-replication-lag-alarms apply` requires
`EDGE_READ_REPLICA_PARAM`, `REDIS_REPLICATION_GROUP_ID` and a non-empty `EDGE_INSTANCE_IDS`, and
**aborts** if the switch reads `true`, if it cannot read the switch at all, or if the instance
list is empty. It saves each alarm's full definition to a file before deleting, asserts the
delete readback, and its `rollback` recreates them from those files. If this environment does read
from a replica, the gate stops you — set the switch and re-derive rather than forcing the delete.

**The EdgeRole grant is already applied in step 2a, and the ordering is enforced by the tool**:
`s3-edge-bundle apply` reads the effective permission and refuses to upload the bundle until it
says `allowed`. Without the grant every edge box writes a warning instead of the metric, the
metric is permanently absent, the alarm is `notBreaching` so it never fires, and the outcome is
indistinguishable from never having applied this kit. `iam/edge-putmetricdata.json` is the exact
document; an equivalent grant under any other policy name is accepted rather than duplicated,
because both the apply and the verification read the role's *effective* decision.

**The golden-image builder pair** only matters if this environment rebuilds the guest rootfs from
this repository. Its asset sha moved because `image.py` packs the whole repository and this range
changes shipped source; the builder itself did not change. If you do not rebuild rootfs here,
record the decision and skip it.

One stack file produces no resource change under this configuration, which is intended rather
than an omission: `tenant_query_rollout.py` changes preflight remediation text only.

## Step 5 — Does a fresh machine need to validate this?

Yes, for the edge tier: the edge Launch Template changed, so let **one** new edge instance boot
on the new version with no hot-fix and watch three signals — the decoded UserData contains no
`{{ }}`, the box reaches `/healthz` 200 inside the grace period, and a real `/ws/` request returns
an HTTP status rather than a reset. That new instance is also what binds the `#625` metric check:
its own `LaunchTime` is the start of the metric window.

The host tier now needs one too. `init-host.sh` changed in this range, so the host Launch Template
carries a new version and the content-addressed bootstrap object moved (see Step 3b). Let **one**
new host launch from the promoted version and watch three signals: the decoded user data contains no
`{{ }}`, the instance registers into `openclaw-hosts`, and its ASG lifecycle hook completes with
CONTINUE rather than a heartbeat timeout. Running hosts are deliberately left alone — the step ends
at promote and issues no instance refresh — so a hot-fixed live host still covers the rest.

## Step 6 — Verification

`manifest.json` `verifications[]` carries all 36 checks with exact `action`, `observable`,
`pass_when` and `fail_when`: 25 read-only, 6 lifecycle, 5 optional. Run every read-only check.

Three of the optional five never pass by design — they exist so a gap is counted instead of hidden:
`v-659-failed-entry-not-exercised` (reaching `status=failed` needs the deadline fence to fire
mid-restore, and the eight tiers have a lower bound equal to `exec_sec`, so the state cannot be
manufactured from outside), `v-660-live-8081-hole-not-exercised` (opening the hole would widen a
running fleet-wide egress policy), and `v-622-live-rule-count-stable`, which reports INCONCLUSIVE
when the host has never failed a reconcile rather than manufacturing a failure by writing the
fleet-wide desired-state parameter.

Rules the checks follow, each worth knowing before you read a result:

- A check asserts the **specific** fixed behaviour. A 401/403 is reported INCONCLUSIVE, never
  pass — an auth failure never reaches the fixed handler.
- Anything that can write is a lifecycle check with an isolated target and an executable
  cleanup. `#631` probes with a documentation-range CIDR and revokes inline if the regression is
  live; `#615` names the exact undo for each of its two routes.
- Log checks are bounded by a recorded mark and read **every** source the signal can land in
  (journald and `error.log`), and they discover the systemd unit rather than hardcoding it — it
  is `claw-edge` here, and a wrong unit makes the check return 0 and pass unconditionally.
- `#625` reads the role's effective permission via `simulate-principal-policy`, and binds the
  metric window to the new instance's `LaunchTime`, so an older datapoint or another box's
  datapoint cannot pass it.
- `#642` needs a live read, not a file check: the write is a **whole-array replacement**, so the
  single value passed in becomes the only origin that tenant may use, and `*` means the Origin
  check is off fleet-wide. Read the parameter, then confirm the host logged the matching one of
  three distinct branches.

Invariants to check independently of any fix: no tenant stuck in `creating`; no
`assignment=failed` while the tenant reads `running`; `used_vcpu <= cap` on every host; and no
tenant id owning more than one Firecracker process (that last one is `#617`'s live check).

## Step 7 — Teardown, one id at a time

Real hosts carry hundreds of real tenants. Delete **only** the exact ids recorded at create, by
looping that list — never a prefix glob, because a stray pattern here is data loss.

```bash
for id in $RECORDED_IDS; do
  curl -s -X DELETE "$OPENCLAW_API_URL/tenants/$id?keep_data=false" -H "x-api-key: $OPENCLAW_API_KEY"
done
```

`keep_data=true` is the default and is a soft delete — the disk stays. Poll each id to `deleted`,
then confirm over SSM that `/data/firecracker-vms/<exact-id>` is gone with no orphan Firecracker
process. Confirm the real-tenant count is identical before and after.
