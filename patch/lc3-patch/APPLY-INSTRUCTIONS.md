# lc3-patch — apply by reading files, no CloudFormation stack update

Successor to `patch/lc2-patch`. It packages the source range
`979a9cb7e925212e4e3151582b90daf1e8b30a82 .. f3b510de6aa9532852a677de36d4afbd8a5c330b`
— the increment merged as PR #229, PR #234 and PR #235 on the `gateway` branch.

- `base_sha` is lc2-patch's own `patch_sha`, so there is no gap between the two kits and
  nothing is packaged twice.
- `status: MANUAL_REVIEW`. Read Step 4 first: it owns every synthesized CloudFormation
  resource in this range, and each one is a by-hand AWS CLI call you approve individually.
- No step here updates a CloudFormation stack. This deployment has manual changes layered on
  top of its original one, and a stack update would overwrite them.

> **The `lib/apply-resource-ops.sh` helper has NOT passed an independent reliability gate.**
> Two rounds of adversarial review scored it 2.0/10 then 3.0/10 against a bar of 6.5, with four
> classes of critical defect still open. Treat it as a **reference implementation that shows the
> intended sequence**, and drive the change from the by-hand commands in each step, which are
> written to be read and approved one at a time. `plan` and the read-only ops are safe to run.
> What is still wrong with it is listed under "Known defects in the helper" at the end of this
> document — read that before deciding to run `apply` from it.

## What this kit changes, in one screen

| Area | Files | Effect |
| ---- | ----- | ------ |
| Lifecycle fence lease | `create_deadline.py`, `fence_config.py`, `lifecycle_fence.py` | The lease stops being a frozen `1800` and reads SSM `/openclaw/lifecycle/fence-lease-sec` (default `240`, refuses below `210`) with a 60s cache |
| Retry idempotency | `lifecycle_fence.py`, `tenant_service.py` | `client_token` covers `restart` and `delete`; a same-token re-entry answers `202` with `retry_after_sec` and dispatches nothing; a real conflict answers `409` with `retry_after_sec` |
| Lifecycle failure states | `tenant_service.py`, `deadline_executor.py`, `health_check/handler.py`, `host_service.py` | `suspend_failed` split by whether a backup exists; `restore` has no failed terminal state; host death confirmed against EC2, not inferred from a missing table row |
| Egress dry run | `egress_admin_service.py` | `POST /hosts/egress/allow/validate` reports each unavailable protected-network criterion separately |
| Pre-launch validator (**optional**, see the end of Step 6) | `lib/validator/**` (19 files) | Read-only operator tool: 11 data-plane delivery channels + control-plane drift, PASS / FAIL / NOT JUDGED per channel. Supersedes the `patch/validator` copy: the same 20 filenames, with three files carrying fixes that copy does not have (a load-bearing `re.MULTILINE`, a content-addressed test fixture, and a `__pycache__` exclusion that stops a false red on the second run). |

| SSM SendCommand rate (**optional tuning**, Step 4.4) | Lambda environment on `openclaw-api` + `openclaw-lifecycle-consumer` | `SPREAD_MAX_HOSTS_PER_BATCH` 6 -> 3 and `HOST_SELECTION_SCORE_FLOOR` 0.5 -> 0.25, halving the SendCommand calls per create batch. Configuration only, no code change. |

Three CloudFormation resources change, all in Step 4: one new SSM parameter and three IAM
role-policy additions (two `ssm:GetParameter`, one `ec2:DescribeInstances`).

**Nothing in this range touches the Launch Template, host userdata, or any S3-delivered host
script.** Steps 3 and 5 are therefore explicitly no-ops — you do not need to roll the host
fleet or launch a fresh host to accept this kit.

## Step 0 — Discover the environment, then prove the kit is authentic

Run the read-only probe first. Everything downstream reads its output rather than a
hand-typed name.

```bash
export AWS_REGION="us-west-2"   # REPLACE with this deployment's region — always explicit, never ambient
export ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
bash lib/discover-env.sh > environment.json
python3 -c "import json;d=json.load(open('environment.json'));print(json.dumps(d,indent=2))" | head -60
```

### 0.1 — Identify the API by traffic, not by name, and learn what its policy admits

Do this before any probe that calls the API. An account commonly holds several REST APIs with
plausible names — an old one, a redeployed one, a copy from a rehearsal. Picking the wrong one
makes every later check test the wrong system and read as "the fix is not live".

```bash
lib/apply-resource-ops.sh apigw-identify-live plan
```

It is read-only. It ranks every REST API by request count over the last 7 days, then — for the
`api_id` your `environment.json` records — prints the resource policy statement by statement and
tells you what a call that policy would admit has to look like.

Two things to take from its output:

- **A zero-request API is not automatically wrong** (a private control plane can be idle), but an
  API *with* traffic that you were not going to target is a red flag. Reconcile before continuing.
- **Take the api id from the URL your deployed client configuration actually uses**
  (`PRIVATE_API_URL` / `CTRL_API_BASE`), not from the top row of the table. The table is how you
  falsify a wrong choice, not how you make the choice.

A `403` on its own tells you nothing: it is the same answer for the wrong API, for wrong auth, and
for a source the resource policy excludes. That is why the op prints the policy — build the probe
call from its conditions (`aws:SourceVpce` means you must call from inside that VPC endpoint, so a
call from a laptop is 403 no matter what credentials you hold).

The op also prints how SigV4 (`AWS_IAM`) methods are signed **on this tree**: the console BFF
imports `SignatureV4` from `@aws-sdk/signature-v4` and its own header records that the module is
not bundled because the `nodejs20.x` runtime already provides it. So the check is "does the import
succeed on the target runtime", not "is a package installed locally". Three gates that circulate
for this do **not** hold here and will fail on a healthy tree: `deploy/console-bff` has no
`package.json`, the import is `@aws-sdk/signature-v4` and not `@smithy/signature-v4`, and
`deploy/cdk-cli` does not exist on this branch. This kit never updates a stack, so no CDK version
is on its critical path; the synth provenance is already bound in
`resources/cloudformation/*.assembly-index.json`.

Read the CONFIRM block it prints and stop unless the account, region and API identity are the
ones you intend to change. An API identity of `null` means the probe could not prove a live
control-plane API from the client URL — resolve that before applying anything.

Then prove every shipped artifact equals the source commit it claims (SHA-256, not SHA-1):

```bash
python3 - <<'PY'
import hashlib, json, pathlib
m = json.load(open("manifest.json"))
bad = []
for src, v in m["paths"].items():
    art = v.get("artifact")
    if not art:
        continue
    p = pathlib.Path(art)
    if not p.is_file():
        bad.append(f"missing artifact {art}")
        continue
    got = hashlib.sha256(p.read_bytes()).hexdigest()
    if got != v["patch_sha256"]:
        bad.append(f"{art}: {got} != manifest {v['patch_sha256']}")
print("\n".join(bad) if bad else f"all {sum(1 for v in m['paths'].values() if v.get('artifact'))} artifacts match the manifest")
PY
```

A mismatch means the kit was mis-packaged. Stop; do not "fix" it by editing the manifest.

## Step 1 — Back up, per function

Three functions carry code from this range. Resolve their real names from `environment.json`
— do not assume them.

- the control-plane API function (serves the API through its `live` alias),
- the lifecycle consumer function (the SQS consumer; it runs the **same** `api` source tree
  and is the primary executor of async lifecycle actions),
- the health-check function (carries `health_check/handler.py`).

For each one, take both halves of the rollback — the version anchor **and** the actual bytes:

```bash
FN="$API_FN"      # set API_FN / HEALTH_FN from environment.json before running this
aws lambda publish-version --function-name "$FN" --region "$AWS_REGION" \
  --description "pre-lc3-patch anchor" --query Version --output text | tee "backup-$FN.version"
aws lambda get-function --function-name "$FN" --region "$AWS_REGION" \
  --query Configuration.RevisionId --output text | tee "backup-$FN.revision"
URL=$(aws lambda get-function --function-name "$FN" --region "$AWS_REGION" \
  --query Code.Location --output text)
curl -sS "$URL" -o "backup-$FN.zip"
unzip -l "backup-$FN.zip" > "backup-$FN.list"   # capture to a file, then grep the FILE
test -s "backup-$FN.zip" || { echo "empty backup — STOP"; exit 1; }
```

Capture the listing into a file before grepping it. Piping `unzip -l` straight into `grep -q`
under `pipefail` SIGPIPE-kills `unzip` and reports 141 — a false failure on a healthy backup.

Also record which version the API alias currently serves, because the rollback has to cover
both the alias and `$LATEST`:

```bash
aws lambda get-alias --function-name "$FN" --name live --region "$AWS_REGION" \
  --query FunctionVersion --output text | tee "backup-$FN.alias-version"
```

## Step 2 — Fix the running functions (per-file overlay)

This range changed only `.py` files; no dependency manifest moved. So the deps come from the
customer's own package and are never rebuilt here: unzip the live package, replace exactly the
files the manifest names, re-zip.

The seven `api` files go into **both** the API function and the lifecycle consumer. Missing the
consumer produces the worst shape available: the API side honours the new lease parameter while
the consumer silently falls back to its code default, and the two diverge the moment an
operator edits the value.

```bash
# per function: FN and SRC_SUBDIR ("api" for the API + consumer, "health_check" for health)
FN="$API_FN"; SRC=lambda/api      # or FN="$HEALTH_FN"; SRC=lambda/health_check
WORK=$(mktemp -d); unzip -q "backup-$FN.zip" -d "$WORK"

# Prove the live bytes are the pre-patch bytes before overwriting them: each file's
# base_sha256 in the manifest must equal what is in the package. A mismatch means this
# function is NOT at base_sha — stop and reconcile, do not overwrite.
python3 - "$WORK" "$SRC" <<'PY'
import hashlib, json, pathlib, sys
work, src = pathlib.Path(sys.argv[1]), sys.argv[2]
m = json.load(open("manifest.json"))
prefix = "lambda/"
stop = []
for s, v in m["paths"].items():
    art = v.get("artifact") or ""
    if not art.startswith(src + "/"):
        continue
    live = work / art[len(prefix):]
    rel = art[len(prefix):]
    if not live.is_file():
        stop.append(f"{rel}: absent from the live package")
        continue
    got = hashlib.sha256(live.read_bytes()).hexdigest()
    if got != v["base_sha256"]:
        stop.append(f"{rel}: live {got} != base_sha256 {v['base_sha256']}")
print("\n".join(stop) if stop else "live package is at base_sha for every file this kit replaces")
PY

# copy only the manifest-named files, then re-zip the whole tree
python3 - "$WORK" "$SRC" <<'PY'
import json, pathlib, shutil, sys
work, src = pathlib.Path(sys.argv[1]), sys.argv[2]
m = json.load(open("manifest.json"))
n = 0
for s, v in m["paths"].items():
    art = v.get("artifact") or ""
    if not art.startswith(src + "/"):
        continue
    dst = work / art[len("lambda/"):]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(art, dst)
    n += 1
print(f"overlaid {n} file(s)")
PY

(cd "$WORK" && zip -qr ../lc3-$FN.zip .) && mv "$WORK/../lc3-$FN.zip" .
aws lambda update-function-code --function-name "$FN" --region "$AWS_REGION" \
  --zip-file "fileb://lc3-$FN.zip" --query '[LastUpdateStatus,CodeSha256]' --output text
aws lambda wait function-updated --function-name "$FN" --region "$AWS_REGION"
```

Verify the function actually runs, then move the alias. Judge the invoke on
`FunctionError` being absent, not on a 200 body — on a private API a synthetic path returns
404 by routing, which is expected:

```bash
aws lambda invoke --function-name "$FN" --region "$AWS_REGION" \
  --payload '{"httpMethod":"GET","path":"/ping"}' --cli-binary-format raw-in-base64-out \
  /dev/null --query FunctionError --output text        # must print None
aws lambda update-alias --function-name "$FN" --name live --region "$AWS_REGION" \
  --function-version "$(aws lambda publish-version --function-name "$FN" \
      --region "$AWS_REGION" --query Version --output text)"
```

Rollback for the API function covers **both** paths: point the alias back at
`backup-$FN.alias-version`, **and** redeploy `backup-$FN.zip` to `$LATEST` — the SQS event
source mapping binds `$LATEST`, so flipping the alias alone leaves the consumer on new code.

## Step 3 — Fix the future-machine source: nothing to do

No file in this range is delivered to a host, baked into the Launch Template, or pulled from
S3 at boot. There is no future-machine divergence to close.

## Step 4 — The CloudFormation resources, as by-hand CLI

**Run these through the shipped helper, not by hand.** `lib/apply-resource-ops.sh` is the one
executable path for every resource this kit owns, so every executor runs identical code:

```bash
lib/apply-resource-ops.sh <op> plan       # read-only: prints the coordinates it resolved
lib/apply-resource-ops.sh <op> apply      # precheck -> backup -> apply
lib/apply-resource-ops.sh <op> verify     # falsifiable readback
lib/apply-resource-ops.sh <op> rollback   # refuses to run without this run's saved state
```

The six ops, in the order they must run:

| # | op | class | rollback |
| - | -- | ----- | -------- |
| 1 | `iam-api-fence-param-read` | `AUTO_CLI` | `RETAIN` |
| 2 | `iam-health-describe-instances` | `AUTO_CLI` | `RETAIN` |
| 3 | `ssm-fence-lease-param` | `AUTO_CLI` | `RESTORE` |
| 4 | `lambda-api-code` | `AUTO_CLI` | `RESTORE` (alias **and** `$LATEST`) |
| 5 | `lambda-health-code` | `AUTO_CLI` | `RESTORE` |
| 6 | `codebuild-goldenimage-asset-drift` | `MANUAL_CLI_REVIEW` | `RETAIN` (nothing is changed) |

Ops 1–3 come before 4–5 deliberately: the grants and the parameter must exist **before** the
code that reads them, or you convert a soft bug into a silent fallback. Every op reads its
coordinates from `environment.json` and **hard-stops on a missing one** rather than defaulting
— the failure mode of a guessed function or role name is "the command succeeded against the
wrong resource".

If your deployment runs a lifecycle consumer, make sure `environment.json` carries
`consumer_role` and `consumer_function`. Op 1 and op 4 then cover it automatically; without
those fields they print a warning and cover the API side only, which is the shape where the api
honours the new lease value and the consumer silently does not.

The rest of this section documents what each op actually does, so you can read before
approving. `REGION` and `ACCOUNT` come from `environment.json`; the helper substitutes them.

### 4.1 `ssm:GetParameter` for the API and consumer roles — `AUTO_CLI`, `rollback_policy: RETAIN`

```bash
# READ-ONLY first: ask your own account whether the grant is already there.
for ROLE in "$API_ROLE" "$CONSUMER_ROLE"; do    # skip CONSUMER_ROLE if this deployment has no consumer
  [ -n "$ROLE" ] || continue
  aws iam simulate-principal-policy \
    --policy-source-arn "arn:aws:iam::$ACCOUNT:role/$ROLE" \
    --action-names ssm:GetParameter \
    --resource-arns "arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter/openclaw/lifecycle/fence-lease-sec" \
    --query 'EvaluationResults[0].EvalDecision' --output text
done
```

An equivalent grant already in place means **skip** — do not add a duplicate. If it is denied:

```bash
sed -e "s/REGION/$AWS_REGION/" -e "s/ACCOUNT/$ACCOUNT/" \
  iam/lifecycle-fence-lease-param-read.json > /tmp/lc3-fence-param.json
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name lc3-lifecycle-fence-lease-param-read \
  --policy-document file:///tmp/lc3-fence-param.json
```

This grant is **not** rolled back. It is read-only, and removing it would break rolled-back
code that still reads the parameter.

### 4.2 `ec2:DescribeInstances` for the health-check role — `AUTO_CLI`, `rollback_policy: RETAIN`

`ec2:Describe*` does not support resource-level IAM, so the resource is `*` and the statement
is read-only. Simulate first as above, then:

```bash
aws iam put-role-policy --role-name "$HEALTH_ROLE" \
  --policy-name lc3-health-describe-instances \
  --policy-document file://iam/health-describe-instances.json
```

Without it, `_host_is_dead` cannot reach EC2 and the reaper falls back to the hosts-table
clue — which can mark a tenant whose VM is still running as terminal.

### 4.3 The lease parameter — `AUTO_CLI`, `rollback_policy: RESTORE`

Guard the write with a read so a re-run is an idempotent adopt, and never lower the value
below `210`:

```bash
NAME=/openclaw/lifecycle/fence-lease-sec
if aws ssm get-parameter --name "$NAME" --region "$AWS_REGION" \
     --query Parameter.Value --output text 2>/dev/null | tee /tmp/lc3-lease-before; then
  echo "parameter already exists — adopt it; only raise it deliberately"
else
  aws ssm put-parameter --region "$AWS_REGION" --name "$NAME" --type String --value 240 \
    --description "openclaw lifecycle fence lease in seconds. Effective immediately, no redeploy. Read by the api and lifecycle-consumer functions with a 60s in-process cache. Lower bound 210s = the delete action's exec_sec budget."
fi
aws ssm get-parameter --name "$NAME" --region "$AWS_REGION" --query Parameter.Value --output text
```

Rollback is `put-parameter --overwrite` back to `/tmp/lc3-lease-before`, or deleting the
parameter if this call created it (the code then falls back to the same `240` default).

Raising this value is safe. Lowering it below `210` is refused at runtime, so a hand-edit
below the floor silently does not take effect — the effective lease stays `240`.

### 4.4 — Lower the SSM SendCommand call rate (optional tuning, `MANUAL_CLI_REVIEW`)

Apply this when the deployment is hitting `ssm:SendCommand` throttling on create bursts. It is
independent of every other fix here: skipping it leaves the rest intact.

Set `SPREAD_MAX_HOSTS_PER_BATCH=3` and `HOST_SELECTION_SCORE_FLOOR=0.25` on **both**
`openclaw-api` and `openclaw-lifecycle-consumer`. One `SendCommand` goes out per host in a batch,
so capping the spread at 3 hosts takes a batch from 6 calls to 3, with each call carrying
proportionally more tenants. Neither knob is injected by the stack in this range — `core/clients.py`
reads them from the environment with code defaults `6` and `0.5`, so this is a pure configuration
change with no code change.

```bash
lib/apply-resource-ops.sh lambda-env-spread-and-floor plan
lib/apply-resource-ops.sh lambda-env-spread-and-floor apply
lib/apply-resource-ops.sh lambda-env-spread-and-floor verify
```

The equivalent by hand, which is what the op runs:

```bash
REGION="ap-southeast-1"   # replace with this deployment's region
PROFILE="default"         # replace with the profile that reaches it

for FN in openclaw-api openclaw-lifecycle-consumer; do
  aws lambda get-function-configuration --profile $PROFILE --region $REGION \
    --function-name $FN --query 'Environment.Variables' --output json > /tmp/$FN.env.json
  python3 -c "
import json; p='/tmp/$FN.env.json'; d=json.load(open(p))
d['SPREAD_MAX_HOSTS_PER_BATCH']='3'; d['HOST_SELECTION_SCORE_FLOOR']='0.25'
json.dump({'Variables': d}, open(p,'w'))"
  aws lambda update-function-configuration --profile $PROFILE --region $REGION \
    --function-name $FN --environment file:///tmp/$FN.env.json \
    --query 'Environment.Variables.{S:SPREAD_MAX_HOSTS_PER_BATCH,F:HOST_SELECTION_SCORE_FLOOR}'
done
```

**Read the existing `Variables` back and merge — never pass a partial map.**
`update-function-configuration --environment` replaces the map wholesale, so sending only these
two keys deletes every other variable the function holds (table names, bucket names, the deadline
knobs). The loop above reads first and merges for exactly that reason; the shipped op additionally
refuses to write when the readback comes back empty.

**Both functions, or neither.** Doing only one leaves the api side and the consumer side running
different values. The consumer is the primary executor of async lifecycle work, so an api-only
change is the shape where the setting appears applied and is not.

**One thing the by-hand loop does not cover.** `openclaw-api` serves traffic through its `live`
alias, i.e. through a published version whose environment is a frozen snapshot;
`update-function-configuration` writes `$LATEST`, which serves nothing. The consumer is different:
its event source mapping binds `$LATEST`, so its new value is live the moment it is written. So
after the loop above, the consumer has the new values and the api does not.

`verify` reads the version the alias actually serves and will report that as a failure on the api
side — that report is the point, not a bug. Two honest ways forward, pick one and say which:

- accept consumer-side-only (the consumer is where the async create path runs), or
- re-run apply with `OC_PUBLISH_API_ALIAS=1`, which publishes a version and moves the alias.
  Note this republishes the api function's **code** along with its environment, so do it after
  Step 2 has landed and been verified, never before.

Rollback restores each function's pre-change variable map from the readback this op saved, and
restores the alias if it moved. It refuses to run without that saved state.

## Step 5 — Fresh-machine validation: not required

Nothing in this range reaches a host image, the Launch Template or boot-time S3 keys, so a
newly launched host is unaffected and no fleet roll is needed to accept this kit.

## Step 6 — Verification, one falsifiable check per fix

`manifest.json` carries the full definition of each check (`action`, `observable`,
`pass_when`, `fail_when`, `timeout_s`, `cleanup`). Run Phase A first; it writes nothing.

**Phase A — read-only**

| id | fix | pass_when |
| -- | --- | --------- |
| V1 | lease parameter | `get-parameter` returns an integer ≥ 210 |
| V2 | lease permissions | `ssm:GetParameter` allowed for BOTH the api and consumer roles |
| V8 | host-death confirmation | `ec2:DescribeInstances` allowed for the health role |
| V9 | egress dry run | `validate` emits a warning naming `VPC_CIDR` on its own line |

`V2` and `V8` read the IAM simulator. The IAM data plane can lag the simulator by minutes:
re-run rather than trusting one result, and confirm with a real call where you can.

**Phase B — full lifecycle, through the real API**

| id | fix | pass_when |
| -- | --- | --------- |
| V3 | lease is live | the logged effective lease equals the SSM value with `source=ssm` |
| V4 | same-token re-entry | both calls answer `202`, the second carries `retry_after_sec`, and **exactly one** `SendCommand` was issued for that tenant |
| V5 | conflict answer | `409` whose body carries an integer `retry_after_sec > 0` |

For `V3`, judge the lease from the log line, **not** from the tenant row's
`active_lifecycle_until` — the production consumer renews that field while you are reading it,
so the table value is not a stable measurement of the configured lease.

For `V4`, the count of `SendCommand` calls is the whole point. Two calls means re-entry
dispatched a second destructive command; `restart` runs `stop-vm && sleep 2 && launch-vm`, and
the two invocations can interleave around that unlocked `sleep 2`.

**Phase B (optional, heavier)** — `V6` (drive a tenant into `suspend_failed` and take the exit
its row advertises) and `V7` (force a restore past its deadline and watch at least two reaper
sweeps). Watch longer than one sweep interval: an observation window shorter than the retry
cadence proves nothing.

### Last, and entirely optional — the pre-launch validator (`V10`)

This is the final item in the kit and it is **your choice whether to run it at all**. Nothing
above depends on it: every fix in Steps 2 and 4 is fully applied and fully verified by V1–V9
without it. It is an independent second opinion on the environment, not a gate on this patch.

If you skip it, this kit is complete. If you want it:

```bash
# 1. offline, no AWS access, no credentials — just proves the tool itself is sound
python3 -m pytest lib/validator/test -q

# 2. only if you then want a live read-only sweep of the environment
lib/validator/oc-prelaunch-validate            # read-only credentials are enough
```

It reports PASS / FAIL / NOT JUDGED for each of the 11 data-plane delivery channels plus
control-plane drift, per channel rather than as one aggregate verdict. Read a `NOT JUDGED` as
"this deployment does not expose what the check needs", not as a pass.

If you do run the offline half, judge it strictly: "zero tests collected" or "everything
skipped" is a failure, not a green — a zero exit code that ran no assertions proves nothing.
The 19 files under `lib/validator/` run on the operator host only; they are never deployed to
AWS and they never write.

## Step 7 — Teardown, one-to-one, no wildcards

Phase B creates real tenants on a host that already carries hundreds of real ones. Record the
exact ids returned at create and delete only those:

```bash
for T in $RECORDED_IDS; do                       # the exact list, never a prefix glob
  curl -sS -X DELETE "$API/tenants/$T?keep_data=false" -H "x-api-key: $KEY"
done
```

`keep_data` defaults to `true`, which is a soft delete that leaves the disk in place — pass
`keep_data=false` explicitly. Poll each id to `deleted`, then confirm over SSM that
`/data/firecracker-vms/<exact-id>` is gone and no orphan `firecracker` process remains. Only
if something is residual, `rm -rf` the **full** id path — never a prefix.

Confirm the real-tenant count is identical before and after.

## What has not been verified

This kit was generated from the git range above. At the time of packaging:

- no step in it has been executed against a live environment;
- the Phase A and Phase B checks are specified but unrun;
- the source range itself carries the upstream verification recorded on PR #235 (full-tree
  coverage against the published branch, structural parse of all 30 changed files, the
  repository's own mechanical checks, and a `cdk synth` of the reviewed tree).

Treat every command here as a proposal to read and approve, not as a tested script.

## Known defects in the helper (`lib/apply-resource-ops.sh`)

Two rounds of independent adversarial review. Round one found eight defects, all fixed. Round two
found the following, none of them fixed yet. The score went 2.0/10 to 3.0/10 against a bar of 6.5,
so the helper is a reference implementation, not a production tool. Every item below is a reason to
run the by-hand commands instead of `apply`.

**State handling**

1. `STATE_DIR` is keyed only by the op name — not by account, region or ARN. Point
   `environment.json` at a different environment and a rollback can write account A's backup into
   account B's same-named resource.
2. There is a completion marker but no *start* marker. A half-failed apply (api succeeded,
   consumer failed) writes no marker, so a re-run overwrites the original backup and pre-state —
   exactly the rollback point you need.
3. `rollback` preflight only checks that files are non-empty. It does not validate the ZIP, the
   JSON, or the alias state before its first write.

**Coordinates**

4. The environment-knob op still falls back to the literal names `openclaw-api` and
   `openclaw-lifecycle-consumer` when the coordinate is missing, instead of refusing.
5. The IAM op modifies the api role *before* it refuses a missing consumer role, so it can leave
   the two sides asymmetric.
6. `lambda-api-code rollback` accepts a missing consumer coordinate and rolls back the api only.

**Lambda concurrency and publishing**

7. `update-function-code` reads the function `RevisionId` but never passes it, so a concurrent
   change is silently overwritten.
8. `publish-version` pins `--code-sha256` but not the revision, and no `update-alias` passes an
   alias revision.
9. `PublishVersion` freezes the function's *entire* configuration, not just its code. Comparing
   only `CodeSha256` does not prove runtime, layers, timeout, role or VPC config match the version
   the alias currently serves.
10. The alias is read as a single version and its `RoutingConfig` is ignored. Under a weighted
    alias, `verify` can pass while part of the traffic still runs the old version.

**Rollback correctness**

11. The IAM op never deletes a policy it newly created, and does not refuse when its saved state is
    missing.
12. If the SSM op merely *adopted* an existing parameter, rollback still overwrites whatever value
    is there now — including a newer one set deliberately after the apply.
13. The environment rollback forces the old, complete map over the current revision, which deletes
    any variable legitimately added since the apply. It should restore or remove only the two keys
    it changed.

**False passes**

14. The environment `verify` compares only the *count* of variables, not the key names, and skips
    the preservation check entirely when no baseline exists.
15. `codebuild batch-get-projects` returns HTTP 200 with a `projectsNotFound` list for a project
    that does not exist; the op does not check that list, so a missing project can compare equal.
16. The IAM `verify` relies on the policy simulator alone. AWS documents that simulated results can
    differ from the real environment, and the data plane lags the simulator by minutes.
17. If the API Gateway resource policy fails to parse, the op prints a note and exits 0.

**Error handling**

18. Optional coordinate reads use `|| true`, which swallows genuine errors; any `publish-version`
    failure is treated as "nothing changed"; a CloudWatch failure lets `verify` continue.

One thing round two confirmed as correct: the first environment apply does read the complete map,
stops on an empty or malformed readback, and passes `RevisionId` to prevent a racing overwrite. The
gap is that `verify` and `rollback` do not hold themselves to the same standard.
