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

## What this kit changes, in one screen

| Area | Files | Effect |
| ---- | ----- | ------ |
| Lifecycle fence lease | `create_deadline.py`, `fence_config.py`, `lifecycle_fence.py` | The lease stops being a frozen `1800` and reads SSM `/openclaw/lifecycle/fence-lease-sec` (default `240`, refuses below `210`) with a 60s cache |
| Retry idempotency | `lifecycle_fence.py`, `tenant_service.py` | `client_token` covers `restart` and `delete`; a same-token re-entry answers `202` with `retry_after_sec` and dispatches nothing; a real conflict answers `409` with `retry_after_sec` |
| Lifecycle failure states | `tenant_service.py`, `deadline_executor.py`, `health_check/handler.py`, `host_service.py` | `suspend_failed` split by whether a backup exists; `restore` has no failed terminal state; host death confirmed against EC2, not inferred from a missing table row |
| Egress dry run | `egress_admin_service.py` | `POST /hosts/egress/allow/validate` reports each unavailable protected-network criterion separately |
| Pre-launch validator | `lib/validator/**` (19 files) | Read-only operator tool: 11 data-plane delivery channels + control-plane drift, PASS / FAIL / NOT JUDGED per channel |

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

Three resources change. Order matters: the two permission grants and the parameter must exist
**before** the code that reads them, or you convert a soft bug into a silent fallback.

`REGION` and `ACCOUNT` below come from `environment.json`. Substitute them into the shipped
policy documents; do not leave the placeholder text in a policy you put.

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
