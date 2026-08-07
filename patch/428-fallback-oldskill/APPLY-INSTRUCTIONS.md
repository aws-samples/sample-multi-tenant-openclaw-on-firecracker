# Applying Patch 428-fallback-oldskill: gateway release-increment sync (apply-by-reading, no CFN redeploy)

<!-- Status is MANUAL_REVIEW, NOT READY. Do NOT claim "No CDK required." -->

Not fully CDK-free. This kit carries `MANUAL_CLI_REVIEW` operations (a new DynamoDB table
`openclaw-image-jobs` + its two GSIs, the opt-in `OpenClawHostImage` / edge-bundle stacks) and the
CloudFormation resource closure is `PENDING` (by design — see Step 4). Read the MANUAL_CLI_REVIEW /
PENDING notes below before applying. Apply by reading the files in this directory; follow the
steps in order. Running machines are hot-fixed before future-machine sources, and fail-closed
prerequisites (env + IAM grants) go first. Every side-effecting command is a confirmation gate —
print it, wait for the operator's OK, then run. Running `setup.sh` or triggering any CloudFormation
stack update is FORBIDDEN, everywhere, always.

Every runtime-path operation carries an EXECUTABLE `apply_cli`/`verify_cli`/`rollback_cli` in
`manifest.json` that invokes a self-contained tool under `lib/` (declared in `kit_files` with
sha256) — run those, do not hand-transcribe:
- `lib/apply-api-routes.sh` (+ `.py`, + `image-routes.spec.json`) — the 6 NEW API-GW routes.
- `lib/apply-fn-grants.sh` — the NEW Lambda env vars + IAM grants (Step 2 fail-closed prereq).
- `lib/apply-image-jobs-table.sh` — the NEW `openclaw-image-jobs` table + both GSIs + TTL.
- `lib/apply-dispatch-tuning.sh` — the dispatch-consumer Timeout/VisibilityTimeout/BatchSize tuning.
- `lib/overlay-lambda.sh` — the code overlay onto BOTH api-asset functions.

This patch packages the gateway ship commit `a63d7b05` against its parent `f8d7c552`
(the state before this publish). It is grouped by LAYER, not per file:

| Layer | What changed | Fix id |
| --- | --- | --- |
| C-lambda | 22 control-plane Lambda files (8 new image/bootstrap modules + dispatch/tenant/host/scheduling updates) | `#428-c-lambda` |
| A-lt | `init-host.sh` (LT-baked host bootstrap) | `#428-a-lt` |
| B-s3 | 4 S3-pulled host userdata scripts (`launch-vm.sh`, `host-agent.py`, `route_ops.py`, new `provision-host.sh`) | `#428-b-s3` |
| deploy-other | 28 pool-core / edge / console / gateway orchestration files (`setup.sh`, `config.yml.example`, console-bff web, fluent-bit, litellm, wazuh, hardening, `scripts/checks/*`) | `#428-deploy-other` |
| D-cdk | 10 CDK stack files → executable CLI (6 new API-GW routes, api/consumer/health env+IAM grants, new DDB table + 2 GSIs, dispatch timeout/visibility/batch tuning, golden-AMI opt-in, opt-in image/edge stacks) | `#428-d-cdk` |

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

Fail-closed prerequisites FIRST — the new code depends on env vars + IAM grants that a code
overlay alone does NOT deliver (they live in `deploy/stacks/lambdas.py`/`ha_edge.py`, applied by
CDK, not carried in the Lambda package). Apply them via the bundled `lib/apply-fn-grants.sh`
BEFORE overlaying code, or the new image-jobs / bootstrap / lifecycle paths `AccessDenied` or
silently no-op:

```bash
# resolve $ACCOUNT_ID (aws sts get-caller-identity) and $HOST_LT_ID (Step 0) first.
lib/apply-fn-grants.sh apply "$REGION" "$ACCOUNT_ID" "$HOST_LT_ID"
lib/apply-fn-grants.sh verify "$REGION" "$ACCOUNT_ID"
# delivers: IMAGE_JOBS_TABLE + BACKUP_BUCKET env on api + consumer; DDB TransactWriteItems/
# ConditionCheckItem on version-snapshots/image-jobs/tenants/hosts; image-jobs RW (+index/*);
# backup-bucket read + KMS decrypt; ec2 Describe/ModifyLaunchTemplate (api, for /bootstrap/promote);
# consumer snapshot/hosts/tenants transacts + backup read; health_fn reaper transact. RETAIN on rollback.
```

**C-lambda (overlay, reuse the live package's compiled deps — do NOT prebuild a fat zip).**
`openclaw-api` has arm64 native wheels; freezing your own dep versions onto it is an unrequested
change. The bundled `lib/overlay-lambda.sh` downloads the live package, publishes a backup version,
overlays ONLY this kit's first-party `lambda/api` tree, re-zips, and `update-function-code`s.

CRITICAL — overlay BOTH functions built from the `deploy/lambda/api` asset. `openclaw-api` AND
`openclaw-lifecycle-consumer` load the SAME asset; overlaying only the api function leaves the
queued create/delete/suspend/restore path (the consumer) on stale code. Both ops are declared on
`deploy/lambda/api/handler.py` in `manifest.json`.

```bash
# api function (foreground request path). Emits the backup-zip path on its last line — capture it.
API_BACKUP=$(lib/overlay-lambda.sh apply openclaw-api lambda/api "$REGION" | tail -1)
# lifecycle consumer (queued lifecycle path) — SAME asset, second function.
CONSUMER_BACKUP=$(lib/overlay-lambda.sh apply openclaw-lifecycle-consumer lambda/api "$REGION" | tail -1)
# verify each imports the new image_*/bootstrap_* modules cleanly (FunctionError=None):
lib/overlay-lambda.sh verify openclaw-api "$REGION"
lib/overlay-lambda.sh verify openclaw-lifecycle-consumer "$REGION"
# rollback (REDEPLOY_ZIP): lib/overlay-lambda.sh rollback <fn> "$API_BACKUP"/"$CONSUMER_BACKUP" "$REGION"
```

Pure-source functions (`scaler`, `health_check`, `tenant_stats`) ship as full trees under
`lambda/` too — for these a prebuilt zip is fine (no third-party deps): `zip -r` the function
dir and `update-function-code`; rollback = `REDEPLOY_ZIP` of the backup. NOTE `health_check` also
needs its new reaper IAM grant — applied by the grants step below, not the code overlay.

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

## Step 4 — API routes + CDK stack changes → executable CLI (review-gated, no stack redeploy)

Every runtime-path op below carries a concrete `apply_cli`/`verify_cli`/`rollback_cli` in
`manifest.json` that invokes a bundled `lib/` tool — run those, don't hand-transcribe. **NETWORK
changes are DESCRIBE-ONLY for the AI** — run only `describe`, present the command, STOP for approval.

**CloudFormation resource closure is PENDING in this kit** (`manifest.cloudformation.status`) — by
design. The 6 new API routes are additive and delivered through the bundled typed route applier
(below); the rest of the diff's API-Gateway churn is ~53 identical OPTIONS-preflight modifications
(the `If-Match`/`Idempotency-Key` header widening). Owning the full closure the validator's way is
all-or-nothing over ~93 resources including those 53, so this kit delivers the additive routes via a
runnable helper and leaves the closure honestly PENDING rather than faking a partial capture.

**API-GW routes (the functional core — 6 new routes; without this the new endpoints 403/404).**
The control-plane API is `apigw.RestApi` with EXPLICIT per-route methods (not `{proxy+}`), so a code
overlay alone does NOT make the new endpoints reachable. `lib/apply-api-routes.sh` (a stateful,
idempotent, crash-resumable applier bundled in this kit) reads `lib/image-routes.spec.json` and, for
each of the 6 routes, creates the API-GW resource + method + `AWS_PROXY` integration (copied from a
live template route, so the Lambda ARN + apiKeyRequired are inherited, never hardcoded) + an
OPTIONS/CORS preflight carrying `If-Match`+`Idempotency-Key`, then issues ONE `create-deployment` and
repoints the `v1` stage.

```bash
# $API_ID = the RestApi physical id (Step 0: get-rest-apis, name "openclaw-orchestrator").
lib/apply-api-routes.sh plan   lib/image-routes.spec.json "$API_ID" v1 "$REGION"   # dry preview
lib/apply-api-routes.sh apply  lib/image-routes.spec.json "$API_ID" v1 "$REGION"   # gated (type APPLY)
lib/apply-api-routes.sh verify lib/image-routes.spec.json "$API_ID" v1 "$REGION"
# rollback restores the pre-apply stage deployment + deletes the routes this run created.
```

- **`AUTO_CLI` — apply via the bundled tool:**
  - `lambdas.py` (routes) — the 6 routes above (`lib/apply-api-routes.sh`).
  - `lambdas.py` (env+IAM) — `lib/apply-fn-grants.sh` (Step 2 fail-closed prereq). RETAIN.
  - `lambdas.py` (dispatch) — `lib/apply-dispatch-tuning.sh apply "$REGION"`: consumer Timeout→900,
    `openclaw-lifecycle.fifo` VisibilityTimeout→960 (`>` timeout), consumer ESM BatchSize→1.
    Rollback `RESTORE` to 180/180/10.
  - `compute.py` / `observability.py` — the runtime IAM/env these describe is delivered by
    `apply-fn-grants.sh`; the residual (log groups/alarms) is `RETAIN`, additive.
  - `_helpers.py` / `app.py` — synthesis-only wiring; no standalone runtime resource; `NONE`.
  - `network_vpc.py` — comment/contract-only; confirm the synth delta is empty (DESCRIBE-only).
  - `ha_edge.py` — host LT golden-AMI opt-in (`resolve:ssm`); handled by the Step-3 A-lt LT path,
    plus the api-fn `ec2:Describe/ModifyLaunchTemplate` grant delivered by `apply-fn-grants.sh`.

- **`MANUAL_CLI_REVIEW` — bundled tool, review before running:**
  - `storage.py` — NEW DynamoDB `openclaw-image-jobs` (HASH `job_id`, TTL `expires_at`) + the TWO
    GSIs the code actually queries — `gsi_idempotency` (instance_id + idempotency_key) and
    `gsi_host_created` (instance_id + created_at). Idempotent adopt-or-create migration:
    ```bash
    lib/apply-image-jobs-table.sh apply  "$REGION"    # creates table + both GSIs (waits ACTIVE each)
    lib/apply-image-jobs-table.sh verify "$REGION"    # asserts both GSIs + TTL
    # rollback (RESTORE): deletes ONLY a table this run created; an adopted pre-existing table = RETAIN.
    ```
  - `host_image.py` — NEW `OpenClawHostImage` EC2 Image Builder pipeline. Opt-in
    (`host.golden_ami.build_pipeline=false` by default) — the running system is untouched; standing
    it up by hand is additive and `RETAIN`. Not required for the runtime path; review before creating.
  - `edge_bundle.py` — NEW edge-bootstrap asset-bundling helper. Additive, review-gated; `RETAIN`.
    Not required for the runtime request path.

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
