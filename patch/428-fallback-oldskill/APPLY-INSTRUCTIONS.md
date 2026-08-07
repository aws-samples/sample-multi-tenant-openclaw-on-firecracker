# Applying Patch 428-fallback-oldskill: gateway release-increment sync (apply-by-reading, no CFN redeploy)

<!-- Status is MANUAL_REVIEW, NOT READY. Do NOT claim "No CDK required." -->

Not fully CDK-free. This kit carries `MANUAL_CLI_REVIEW` operations (a new DynamoDB table
`openclaw-image-jobs` + GSI, the new `OpenClawHostImage` / edge-bundle stacks) and the
CloudFormation resource closure is `PENDING` (see Step 4). Read the MANUAL_CLI_REVIEW /
PENDING notes below before applying. Apply by reading the files in this directory; follow the
steps in order. Running machines are hot-fixed before future-machine sources, and fail-closed
prerequisites go first. Every side-effecting command is a confirmation gate — print it, wait
for the operator's OK, then run. Running `setup.sh` or triggering any CloudFormation stack
update is FORBIDDEN, everywhere, always.

This patch packages the gateway ship commit `a63d7b05` against its parent `f8d7c552`
(the state before this publish). It is grouped by LAYER, not per file:

| Layer | What changed | Fix id |
| --- | --- | --- |
| C-lambda | 22 control-plane Lambda files (8 new image/bootstrap modules + dispatch/tenant/host/scheduling updates) | `#428-c-lambda` |
| A-lt | `init-host.sh` (LT-baked host bootstrap) | `#428-a-lt` |
| B-s3 | 4 S3-pulled host userdata scripts (`launch-vm.sh`, `host-agent.py`, `route_ops.py`, new `provision-host.sh`) | `#428-b-s3` |
| deploy-other | 28 pool-core / edge / console / gateway orchestration files (`setup.sh`, `config.yml.example`, console-bff web, fluent-bit, litellm, wazuh, hardening, `scripts/checks/*`) | `#428-deploy-other` |
| D-cdk | 10 CDK stack files → manual CLI equivalents (new DDB table + GSI, dispatch timeout/visibility/batch tuning, golden-AMI opt-in, new image/edge stacks) | `#428-d-cdk` |

The authoritative machine-readable contract is `manifest.json` (per-path `layer`,
`base_sha256`, `patch_sha256` == the shipped artifact, `operations[].class`, and
`fixes[]`/`verifications[]`). Echo it before applying.

## Step 0 — Probe: gather the real values (run these first)

```bash
# Region / account / identity
aws sts get-caller-identity
REGION="${AWS_REGION:-ap-southeast-1}"
# Is the control-plane API private or public API Gateway? (decides which routing paths apply)
# Host IP(s) + transport: hosts in a private subnet reach you only via SSM (no direct SSH).
# Discover the assets bucket + the exact S3 prefix hosts pull userdata from — never guess:
aws ssm start-session --target <host-instance-id> --region "$REGION"   # then, on the host:
#   grep -o 's3://[^ ]*deployment/scripts[^ ]*' /var/log/openclaw-init.log | sort -u
# Which Lambda is the control-plane API? (name contains ApiHandler / openclaw-api)
aws lambda list-functions --region "$REGION" \
  --query "Functions[?contains(FunctionName,'ApiHandler')||contains(FunctionName,'openclaw-api')].FunctionName" --output text
# Host ASG + its pinned LT version (for the A-lt path):
aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --query "AutoScalingGroups[?contains(AutoScalingGroupName,'host')].[AutoScalingGroupName,LaunchTemplate,MixedInstancesPolicy]" --output json
```

## Step 1 — Impact assessment (write this before changing anything)

- **Affected:** the control-plane Lambda, the host fleet (ASG + live hosts), the assets
  bucket `deployment/scripts/` objects, the edge/console-bff assets, and (for D-cdk) the
  DynamoDB tables + dispatch SQS/Lambda config. No tenant data plane is modified by this patch.
- **Symptom / reason to apply:** this is a release-increment sync (image build + bootstrap
  version flow, dispatch tuning, golden-AMI opt-in), not an incident hotfix — apply it to
  bring a running gateway up to `a63d7b05` without a CloudFormation redeploy that would
  overwrite the operator's manual changes.
- **Root cause / intent:** see each `fixes[].summary` in `manifest.json`.
- **Expected post-fix state:** the per-layer verifications in Step 6 all pass.

## Step 1.5 — Full change list + anti-revert hash gate (RUN BEFORE ANY WRITE)

This gateway is applied patch-after-patch; a blind replacement can DOWNGRADE a running file
that is already newer. Prove that is not happening, per file.

```bash
# Print the full change list the operator is about to touch:
python3 - <<'PY'
import json
m=json.load(open("manifest.json"))
for p,v in sorted(m["paths"].items()):
    if v["artifact_status"]=="SHIPPED":
        print(f'{v["layer"]:12} {p}\n   base={v["base_sha256"][:16]} patch={v["patch_sha256"][:16]} -> {v["artifact"]}')
PY
```

For each shipped file, hash what is LIVE and branch:

- `LIVE == patch_sha256` → already applied, **SKIP** (idempotent).
- `LIVE == base_sha256` → clean apply, proceed.
- `LIVE == neither` → the live file DIVERGED. **STOP. Do NOT overwrite.** Show the operator
  `diff` and ask whether the shipped version is actually newer before proceeding.

## Step 2 — Hot-fix the RUNNING machines (restore/advance service now)

Fail-closed prerequisites first (any IAM grant the new code depends on — apply the D-cdk
`AUTO_CLI` IAM/env ops from Step 4 that a running function needs BEFORE overlaying code).

**C-lambda (overlay, reuse the live package's compiled deps — do NOT prebuild a fat zip).**
The `openclaw-api` function has arm64 native wheels; freezing your own dep versions onto it is
an unrequested change. Download the live package, overlay only the first-party source, re-zip:

```bash
FN=$(aws lambda list-functions --region "$REGION" \
  --query "Functions[?contains(FunctionName,'ApiHandler')||contains(FunctionName,'openclaw-api')].FunctionName" --output text)
# backup anchor: record the live RevisionId + CodeSha256, publish a version, download the zip
aws lambda get-function --function-name "$FN" --region "$REGION" \
  --query '{rev:Configuration.RevisionId,sha:Configuration.CodeSha256}'
aws lambda publish-version --function-name "$FN" --region "$REGION" --query Version   # rollback anchor
URL=$(aws lambda get-function --function-name "$FN" --region "$REGION" --query Code.Location --output text)
curl -s "$URL" -o /tmp/openclaw-api-live.zip
work=$(mktemp -d); (cd "$work" && unzip -q /tmp/openclaw-api-live.zip)
# delete ONLY the first-party source dirs the overlay replaces, then copy this kit's lambda/ tree in:
rm -rf "$work/core" "$work/services" "$work/handler.py"
cp -a lambda/api/. "$work/"
(cd "$work" && zip -qr /tmp/openclaw-api-new.zip .)
aws lambda update-function-code --function-name "$FN" --zip-file fileb:///tmp/openclaw-api-new.zip --region "$REGION"
aws lambda wait function-updated --function-name "$FN" --region "$REGION"
# invoke-verify (FunctionError=None) then flip the alias if one is used; rollback = re-deploy the
# downloaded backup zip AND re-point the alias (dispatch ESM binds $LATEST, so cover both).
```

Pure-source functions (`scaler`, `health_check`, `tenant_stats`) ship as full trees under
`lambda/` too — for these a prebuilt zip is fine (no third-party deps): `zip -r` the function
dir and `update-function-code`; rollback = `REDEPLOY_ZIP` of the backup.

**B-s3 (hot-replace on live hosts).** Only after Step-1.5 cleared each file. Back up, replace
via SSM (hosts are usually private — no direct SSH), validate by type, diff-guard:

```bash
# push a file to a host over SSM (base64 transport; explicit interpreter ignores the exec bit,
# and S3 delivery carries no unix mode — 0644 is correct for these interpreter-invoked scripts):
B64=$(base64 < host-scripts/launch-vm.sh.patched)
aws ssm send-command --instance-ids <host> --document-name AWS-RunShellScript --region "$REGION" \
  --parameters commands="cp /home/ubuntu/launch-vm.sh /home/ubuntu/launch-vm.sh.bak.428; echo $B64 | base64 -d > /home/ubuntu/launch-vm.sh; bash -n /home/ubuntu/launch-vm.sh"
# for *.py use: python3 -m py_compile ; diff-guard against the .bak; rollback = restore the .bak.428
```

## Step 3 — Fix the FUTURE-machine source (S3 + Launch Template)

**B-s3 → assets bucket.** Upload each patched script to a TEMP key, verify, then PROMOTE
(never overwrite the canonical object blind — a bad object fails every new host at once). Keep
the old version id for rollback. Use cross-platform sha256 (`shasum` is absent on Linux/AL2023):

```bash
_sha(){ if command -v sha256sum >/dev/null; then sha256sum "$1"|awk '{print $1}';
  elif command -v shasum >/dev/null; then shasum -a 256 "$1"|awk '{print $1}';
  else echo FATAL-no-sha256 >&2; return 1; fi; }
BUCKET="$ASSETS_BUCKET"; PFX=deployment/scripts   # set ASSETS_BUCKET from Step 0
for f in launch-vm.sh host-agent.py route_ops.py provision-host.sh; do
  aws s3api head-object --bucket "$BUCKET" --key "$PFX/$f" --query VersionId --output text --region "$REGION" 2>/dev/null || echo "new object $f"
  aws s3 cp "host-scripts/$f.patched" "s3://$BUCKET/$PFX/$f.patch-428.tmp" --region "$REGION"
  aws s3 cp "s3://$BUCKET/$PFX/$f.patch-428.tmp" /tmp/verify --region "$REGION"
  diff /tmp/verify "host-scripts/$f.patched" && aws s3 cp "s3://$BUCKET/$PFX/$f.patch-428.tmp" "s3://$BUCKET/$PFX/$f" --region "$REGION"
done
```

**A-lt (`init-host.sh`) — LT-baked, shipped in FULL under `launch-template/`.** A running host
got this script from its Launch Template at boot; it does NOT pull it from S3, so changing the
repo file does nothing until the LT is rolled. Do NOT hand-wrangle base64/gzip/the 16KB limit,
and NEVER base64 the raw template (it has ~31 `{{PLACEHOLDER}}` tokens CDK substitutes at synth
— booting on literal `{{...}}` fails). Decode the CURRENT LT version's already-rendered
UserData, apply only this patch's hunk to it, re-bake:

```bash
# 1. pull the ASG-pinned LT version, decode its rendered UserData (NOT the shipped template).
# 2. apply only the changed init-host hunk to the decoded rendered text.
# 3. create-launch-template-version with the re-baked UserData (verify <16KB).
aws ec2 create-launch-template-version --launch-template-name openclaw-host-lt \
  --source-version <cur> --launch-template-data '{"UserData":"<base64-of-rebaked-rendered-userdata>"}' --region "$REGION"
# 4. point the ASG at the new version (modify --default-version is NOT enough — the ASG pins a
#    specific numeric version). For a MixedInstancesPolicy, edit the LT spec inside it instead.
aws autoscaling update-auto-scaling-group --auto-scaling-group-name <host-asg> \
  --launch-template LaunchTemplateName=openclaw-host-lt,Version=<new> --region "$REGION"
# 5. existing instances do NOT auto-update; roll them only via a CONTROLLED instance refresh,
#    gated on the Step-6 fresh-host verify:
#    aws autoscaling start-instance-refresh --auto-scaling-group-name <host-asg> \
#      --preferences MinHealthyPercentage=90,InstanceWarmup=300 --region "$REGION"
# rollback (LT_REVERT): update-auto-scaling-group back to <cur>.
```

**deploy-other → in place.** `setup.sh`, `config.yml.example`, console-bff web assets, edge
fluent-bit, litellm bring-up, wazuh, hardening, `scripts/checks/*` are shipped code that runs on
the edge/console/build hosts. Replace each from its `host-scripts/<path>.patched` artifact on the
host/asset that serves it (same backup → replace → syntax-check → diff-guard flow as B-s3), and
re-run the specific consumer (e.g. restart the console-bff service, re-run fluent-bit install).
Do NOT run `setup.sh` — copy the changed files, do not re-orchestrate.

## Step 4 — CDK stack changes → manual CLI equivalents (review-gated, no stack redeploy)

Per `operations[].class` in `manifest.json`. **NETWORK changes are DESCRIBE-ONLY for the AI** —
run only `describe`, present the impact + proposed command, and STOP for explicit approval.

**CloudFormation resource closure is PENDING in this kit** (`manifest.cloudformation.status`).
A commit-bound `cdk synth --all` on both refs is feasible offline (`network.mode=self_managed` +
`aws:cdk:bundling-stacks=[]` + a concrete account/region make it Docker-free), but this fallback
kit classifies the 10 D-cdk paths from the source diff instead of a captured per-resource A/M/D
closure. Treat the classifications below as reviewed guidance, and re-derive the exact resource
set from a captured synth before applying anything topology-touching.

- **`AUTO_CLI` — apply as safe CLI:**
  - `lambdas.py` — dispatch consumer tuning: `update-function-configuration --timeout 900`;
    `set-queue-attributes VisibilityTimeout=960` (must be `>` the function timeout);
    `update-event-source-mapping --batch-size 1`. Rollback `RESTORE` to 180/180/10.
  - `compute.py` / `observability.py` — IAM grants + Lambda env + log-group/alarm wiring for
    the image/bootstrap services: inline `put-role-policy` + `update-function-configuration`
    env; read-only grants and log groups are `RETAIN` (do not roll back a fail-closed prereq).
  - `_helpers.py` / `app.py` — synthesis-only wiring (SSM parameter-name helper; register the
    `OpenClawHostImage` stack). No standalone runtime resource; `rollback_policy: NONE`.
  - `network_vpc.py` — in-range change is a comment/contract reference only; confirm the synth
    delta is empty (DESCRIBE-only), take no action.
  - `ha_edge.py` — host LT gains the golden-AMI opt-in branch (`resolve:ssm` at launch). Handled
    by the Step-3 A-lt LT-version path, not a stack deploy; rollback `LT_REVERT`.

- **`MANUAL_CLI_REVIEW` — exact by-hand CLI, forces `status: MANUAL_REVIEW`:**
  - `storage.py` — NEW DynamoDB table `openclaw-image-jobs` + a by-`instance_id` GSI. This is a
    controlled online migration, not unpatchable:
    ```bash
    # idempotent adopt-or-create, then add the GSI, each waiting ACTIVE (DDB builds one GSI/update):
    aws dynamodb describe-table --table-name openclaw-image-jobs --region "$REGION" 2>/dev/null \
      || aws dynamodb create-table --table-name openclaw-image-jobs --region "$REGION" \
           --billing-mode PAY_PER_REQUEST \
           --attribute-definitions AttributeName=job_id,AttributeType=S AttributeName=instance_id,AttributeType=S \
           --key-schema AttributeName=job_id,KeyType=HASH
    aws dynamodb wait table-exists --table-name openclaw-image-jobs --region "$REGION"
    # TTL on expires_at:
    aws dynamodb update-time-to-live --table-name openclaw-image-jobs --region "$REGION" \
      --time-to-live-specification "Enabled=true,AttributeName=expires_at"
    # by-instance GSI (only if describe shows it absent):
    aws dynamodb update-table --table-name openclaw-image-jobs --region "$REGION" \
      --attribute-definitions AttributeName=instance_id,AttributeType=S \
      --global-secondary-index-updates '[{"Create":{"IndexName":"by-instance","KeySchema":[{"AttributeName":"instance_id","KeyType":"HASH"}],"Projection":{"ProjectionType":"ALL"}}}]'
    # verify: describe-table shows TableStatus=ACTIVE and the GSI IndexStatus=ACTIVE.
    # rollback (RESTORE): a NEWLY-created table -> delete-table openclaw-image-jobs; an adopted
    #   pre-existing table -> RETAIN (do not delete).
    ```
  - `host_image.py` — NEW `OpenClawHostImage` EC2 Image Builder pipeline/component/recipe. Opt-in
    (`host.golden_ami.build_pipeline=false` by default), so the running system is untouched;
    standing the pipeline up by hand is additive and `RETAIN` on rollback. Review before creating.
  - `edge_bundle.py` — NEW edge-bootstrap asset-bundling helper feeding the edge LT/bootstrap
    flow. Additive, review-gated (touches edge bootstrap topology); `RETAIN`.

There is NO `UNPATCHABLE` operation in this patch. This whole layer is flagged for codex review.

## Step 5 — Post-fix: fresh-machine validation?

This patch touched the LT (`init-host.sh`) and the future-machine S3 scripts, so a fresh-machine
check IS required: after Step 3, launch ONE new host on the new LT / new S3 scripts and let it
boot with NO hot-fix (verification `v-428-lt-newhost`). It must register into `openclaw-hosts`
on its own with no `{{ }}` in its decoded UserData and an ASG lifecycle CONTINUE. Only after that
one host proves clean should a controlled instance refresh roll the change through the fleet.

## Step 6 — Guided verification plan (a FALSIFIABLE check per layer)

Run exactly the verifications `manifest.verifications[]` lists, grouped by `phase`; judge each by
its own `pass_when`/`fail_when`. Use the REAL table/field names (`openclaw-tenants`,
`openclaw-hosts`, `openclaw-image-jobs` — confirm on a same-arch env before querying). Pick the
control-plane API by the configured client URL + real call-site auth + a successful real request,
not by name.

```bash
python3 - <<'PY'
import json
m=json.load(open("manifest.json"))
for v in m["verifications"]:
    print(f'[{v["phase"]}] {v["id"]} ({v["fix_id"]}): {v["action"]}')
    print(f'    PASS if: {v["pass_when"]}   FAIL if: {v["fail_when"]}   timeout {v["timeout_s"]}s')
PY
```

- **C-lambda** (`v-428-lambda-codesha`, `v-428-lambda-invoke`, Phase A): `CodeSha256` differs from
  the pre-apply backup, and a synthetic image/bootstrap-route invoke returns `FunctionError=None`
  (proves the new `image_*` / `bootstrap_*` modules import cleanly).
- **A-lt** (`v-428-lt-newhost`, Phase B): the one fresh host from Step 5 registers healthy.
- **B-s3** (`v-428-s3-syntax` Phase A, `v-428-s3-live` Phase B): replaced files byte-match the
  shipped `.patched` + pass per-type syntax checks; one real tenant reaches `running` on a
  hot-fixed host.
- **deploy-other** (`v-428-other-syntax`, Phase A): every replaced file matches its artifact and
  passes its per-type check.
- **D-cdk** (`v-428-cdk-imagejobs`, `v-428-cdk-dispatch`, Phase A): `openclaw-image-jobs` ACTIVE
  with the `by-instance` GSI ACTIVE and TTL on `expires_at`; the dispatch consumer shows
  `Timeout=900`, `VisibilityTimeout=960` (`>` timeout), `BatchSize=1`.

## Step 7 — Precise teardown (one-to-one, ZERO wildcards)

Real hosts carry hundreds of real tenants (`thr*`/`t-*`) — a stray `rm -rf` glob is data loss.
Any test tenant created in Step 6 uses a unique zero-padded prefix; delete ONLY the exact ids
returned at create (loop the recorded list, never a prefix glob).

```bash
for t in "${IDS[@]}"; do case "$t" in "$RUN"-*)
  curl -s -X DELETE "$API/tenants/$t?keep_data=false" -H "x-api-key:$KEY" ;;  # default keep_data=true is soft-delete
esac; done
# poll each to deleted; per host SSM-confirm /data/firecracker-vms/<exact-id> is gone + no orphan fc;
# residual only -> sudo rm -rf /data/firecracker-vms/<exact-id>  (FULL id, never a wildcard).
# Terminate the single Step-5 test host and revert the ASG to the prior LT version if not keeping it.
# Confirm the real-tenant count is identical before/after.
```
