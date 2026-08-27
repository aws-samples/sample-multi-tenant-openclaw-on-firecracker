# fix-0826 — apply by reading files, no CloudFormation stack update

`manifest.json` is the single source of truth. When this document and the manifest disagree
about a hash, a path or a logical id, the manifest wins.

- range: gateway `c9fd494ff4a76929f205f52464047a9185c7c49a` → the head of this branch, which
  carries bb through `3c4494e8` (the #668 egress increment)
- the base is the `patch_sha` of `patch/lifecycle-op-patch`, i.e. **the revision this
  environment is actually on**. 303 paths, 32 fixes, 52 verifications.
- this is the **only** kit to apply for that hop. It is one hop, not a chain:
  `lifecycle-op-patch` → `lc2-patch`.
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

Two kits that briefly existed for slices of this same range are **gone from the gateway tree**,
folded in here so an operator is never offered overlapping upgrades for one hop:

| removed kit | what it covered | where it lives now |
| --- | --- | --- |
| `patch/egress-207-patch` | `POST /hosts/egress` `wait=true` answering 200 on an incomplete collection | fix `#657`, same `egress_admin_service.py` bytes |
| `patch/auto-8f86347b` | the same increment plus the SSM `TimeoutSeconds` lower bound | fix `#657` (`params_changed` records the derived value) |

Their directories were deleted rather than their merges reverted, for two reasons. Reverting
would also have undone the product content those merges carried. And a revert does not remove a
`bb-baseline:` anchor — the anchor lives in a commit **message**, and the publish tool scans
messages backwards from the tip, so the reverted commit's anchor survives and keeps asserting that
a withdrawn increment was published. The next publish would then compute its increment from that
stranded anchor and skip the withdrawn range entirely. Deleting the directories leaves the product
content and both markers intact and the anchor chain honest.

## Permissions — read this before Step 0

Two separate questions live here, and conflating them is how a hot-patched Lambda starts throwing
`AccessDenied` in production. **(A) What permission does this kit's code change require?** and
**(B) what does this kit's code call that a stock deployment never granted?**

### A. Permission changes this range introduces: exactly one, and it is already in the kit

Both CloudFormation closures the kit ships (`resources/cloudformation/*.base.json` versus
`*.patch.json`) were compared statement by statement across every `AWS::IAM::Role`, `Policy`,
`ManagedPolicy`, `RolePolicy` and `InstanceProfile`, normalising each statement so a reordering does
not read as a change. Two resources differ, and only one of them is a permission:

| Resource | Difference | What it is |
| --- | --- | --- |
| `OpenClawOrchestrator` / `EdgeRoleDefaultPolicy` | gains `cloudwatch:PutMetricData`, `Resource: "*"`, conditioned on `cloudwatch:namespace` equals `OpenClaw/Edge` | the real new grant — `#625`. Shipped as `iam/edge-putmetricdata.json`, byte-equal to the statement the closure adds. Step 4 applies it. |
| `OpenClawImage` / `GoldenImageBuildRoleDefaultPolicy` | the S3 asset object ARN in an existing `s3:GetObject*/GetBucket*/List*` statement moves to a different `.zip` key | **not** a permission change. It is the same three actions on the same bucket; only the content-addressed asset key rotated. Step 4's golden-image operation re-points it and asserts the build role can read the new key. |

So: **the only permission the code in this range newly needs is `cloudwatch:PutMetricData` on the
edge role, scoped to the `OpenClaw/Edge` namespace, and it is already packaged.** Nothing else in
this hop widens a policy.

### B. Six calls in the shipped code that a stock deployment does not grant

The closure diff above cannot answer this. An ungranted call produces no policy statement, so it
leaves no trace in a template comparison — it only shows up at runtime as `AccessDenied`. Every one
of the 14 Python files this kit ships was therefore walked with `ast`, keyed on the **method** name
rather than the handle (`ssm.put_parameter` and `_ssm_adaptive().get_parameter` are the same API, and
a handle-keyed scan silently misses the second — as an earlier pass of this analysis did), and each
action checked against **all four** policies attached to `ApiHandlerServiceRole` (`DefaultPolicy`,
`OverflowPolicy`, `OverflowPolicy2`, `ApiSelfInvokePolicy`, plus the one managed policy,
`AWSLambdaBasicExecutionRole`) — a CDK policy overflow splits grants across several documents, and
reading only the default one hides most of them. **26 distinct actions, 20 granted, 6 not**, worst
first:

| Action | Call site in the shipped kit | What happens on a stock deployment |
| --- | --- | --- |
| `ssm:GetParameter` | `services/dispatch_service.py:103`, in `_check_andon()` | **all dispatch stops.** This read is deliberately fail-**closed**: `except Exception: return True, f"andon-read-failed: …"`, and `True` means the emergency stop is engaged. So `AccessDenied` here does not degrade dispatch, it halts it — and the reason string names a read failure, not a permission, which is what makes it hard to recognise. The closure's only `ssm:GetParametersByPath` grant is scoped to `parameter/openclaw/lifecycle/deadline-sec/*`; this read is `/openclaw/dispatch/config`, a different prefix **and** a different action. |
| `ssm:PutParameter` | `services/dispatch_service.py:1081`, in `_put_manifest_parts()` | **fatal for the dispatch in flight.** The call is not wrapped, so `AccessDenied` propagates and the batch that was writing its manifest parts fails. |
| `kms:Encrypt` | `services/tenant_service.py:925`, in `mint_device_identity()`, through `core.kms_envelope.encrypt()` → `kms.encrypt(KeyId=CLAWPOOL_CMK_ARN)` | **device identity cannot be minted.** Note the callee is `core/kms_envelope.py`, which this kit does **not** ship — but the caller is shipped, so applying the kit makes shipped code depend on this grant. |
| `kms:GetPublicKey` | `handler.py:3009`, in `_get_clawpool_rsa_public_key()` | **502 on that endpoint only,** and only where `security.clawpool_cmk_enabled` is on — the handler already returns `502 UPSTREAM "kms:GetPublicKey failed"`, so the failure is at least legible. With the feature off the code returns 404 before reaching KMS. |
| `ssm:DeleteParameter` | `services/dispatch_service.py:1099`, in `_delete_manifest_parts()` | **silent leak.** The exception is caught and logged `non-fatal`, so every dispatch leaves its `SecureString` manifest parts behind for good: unbounded parameter growth, and tenant material retained past the run that needed it. |
| `sqs:GetQueueAttributes` | `handler.py:1220`, in `_queue_depth()` | **cosmetic.** `_queue_depth` is fail-soft by construction — on any error it returns `None` rather than 500 — so the four queue-depth fields in the system-info response read `null` and nothing else changes. |

**All six are pre-existing, not introduced by this kit.** Each call was re-run against the same file
at `base_sha`: every one is present there with a byte-identical call expression, only at a different
line (for instance `put_parameter` L987→L1081, `get_queue_attributes` L1209→L1220). This kit does
not create the gap and does not require you to close it. It is written down because applying the kit
does not fix it either, and a reader who saw only section A would conclude the permission surface is
complete.

**What this analysis does not cover, so you do not over-read it.** It is *action*-level. The closure
grants DynamoDB and KMS per resource ARN, so an action marked granted here can still be denied on
the specific resource the code touches. A live example from this same role: `kms:Decrypt` counts as
granted above, but the only statement carrying it is scoped to the **backup** CMK, while
`kms_envelope.decrypt()` decrypts under `CLAWPOOL_CMK_ARN` — a different key. Action-level coverage
is a floor, not a proof. The simulator command below is the thing that answers for your account,
because it evaluates policies rather than reading intent out of a repository.

### Where to add them, if you choose to

Attach to the **API handler's execution role** — the role behind the function this kit's `C-lambda`
operation updates, `ApiHandlerServiceRole` in the closure. `iam/api-handler-dispatch-manifest-and-queue-depth.json`
is the policy document, carrying ten `__TOKEN__` values you resolve against your own environment:

`environment.json` does not carry the role or the queue coordinates, so derive them from the one
thing it does confirm — `lambda_link.function`, the function the API actually invokes. Every value
below comes from the live function's own configuration, which is why this works on an environment
whose resource names do not match the repository's:

```bash
FN=$(python3 -c 'import json;print(json.load(open("environment.json"))["lambda_link"]["function"])')
CFG=$(aws lambda get-function-configuration --region us-west-2 --function-name "$FN")
ROLE_ARN=$(printf '%s' "$CFG" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Role"])')
ROLE=${ROLE_ARN##*/}
echo "function=$FN role=$ROLE"

# READ-ONLY: ask your own account which of the three are actually denied.
# Never assume from this document — run the simulator against the live role.
aws iam simulate-principal-policy --region us-west-2 \
  --policy-source-arn "$ROLE_ARN" \
  --action-names ssm:GetParameter ssm:PutParameter ssm:DeleteParameter \
                 sqs:GetQueueAttributes kms:Encrypt kms:GetPublicKey \
                 ssm:SendCommand ssm:GetCommandInvocation ssm:ListCommandInvocations \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text
```

Read the output this way:

- `ssm:SendCommand`, `ssm:GetCommandInvocation` and `ssm:ListCommandInvocations` **are** in the
  closure. If any of those three comes back denied, that is **deployment drift on your side**, not
  a gap in this repository — stop and reconcile the role before touching anything else.
- `kms:Encrypt` is there because `_put_manifest_parts` writes `Type="SecureString"` and passes no
  `KeyId`, so the default `alias/aws/ssm` key is used. Per the Parameter Store documentation, a
  **standard** `SecureString` write requires `kms:Encrypt` on that key (an **advanced**-tier write
  would require `kms:GenerateDataKey` instead), and the default `aws/ssm` key has no editable key
  policy, so the identity policy is the only place to grant it. If your account replaced the
  default with a customer-managed key, substitute that key's ARN.
- The three actions in the table are the decision. Granting them is a **widening of a production
  role**, so it is deliberately not automated here: no `lib/` script applies this file. If you want
  it, substitute the tokens and attach it by hand, and record the pre-change policy set first.

Every token is resolved from the live function's environment, not from this repository's names. The
four queue URLs are read off the function's own configuration and turned into ARNs by SQS itself,
so a renamed queue resolves correctly and a queue this deployment does not have fails closed rather
than being guessed:

```bash
# derive each value from the live function, then substitute. $CFG is from the block above.
PREFIX=$(printf '%s' "$CFG" | python3 -c 'import json,sys
env=(json.load(sys.stdin).get("Environment") or {}).get("Variables") or {}
print(env.get("DISPATCH_PARAM_PREFIX","/openclaw/dispatch"))')
KMS_ARN=$(aws kms describe-key --region us-west-2 --key-id alias/aws/ssm \
  --query KeyMetadata.Arn --output text)
# The two clawpool CMKs come off the function's own configuration. An empty value means the
# feature is off in this deployment, and its statement is dropped rather than left as a token.
: > clawpool-cmks.pairs
for v in CLAWPOOL_CMK_ARN CLAWPOOL_RSA_CMK_ARN; do
  arn=$(printf '%s' "$CFG" | python3 -c "import json,sys
env=(json.load(sys.stdin).get('Environment') or {}).get('Variables') or {}
print(env.get('$v',''))")
  printf '%s\t%s\n' "$v" "$arn" | tee -a clawpool-cmks.pairs
done
: > queue-arns.pairs
for v in DISPATCH_QUEUE_URL DISPATCH_DLQ_URL LIFECYCLE_QUEUE_URL LIFECYCLE_DLQ_URL; do
  url=$(printf '%s' "$CFG" | python3 -c "import json,sys
env=(json.load(sys.stdin).get('Environment') or {}).get('Variables') or {}
print(env.get('$v',''))")
  if [ -z "$url" ]; then echo "STOP: the live function has no $v"; continue; fi
  arn=$(aws sqs get-queue-attributes --region us-west-2 --queue-url "$url" \
        --attribute-names QueueArn --query Attributes.QueueArn --output text)
  printf '%s\t%s\n' "$v" "$arn" | tee -a queue-arns.pairs
done
python3 -c 'import json,sys
print(json.dumps(dict(l.rstrip("\n").split("\t",1) for l in open("queue-arns.pairs") if l.strip()), indent=2))' \
  > queue-arns.json
cat queue-arns.json
```

`queue-arns.json` is what the substitution below reads. Check it lists all four before continuing:
a queue missing here becomes a token with no value, and the next step refuses rather than emitting a
policy that would attach cleanly and then deny every call it was supposed to allow.

```bash
python3 - "$PREFIX" "$KMS_ARN" <<'PY'
import json, subprocess, sys
prefix, kms_arn = sys.argv[1], sys.argv[2]
env = json.load(open("environment.json"))
arns = json.load(open("queue-arns.json"))     # {"DISPATCH_QUEUE_URL": "arn:...", ...} from above
doc = open("iam/api-handler-dispatch-manifest-and-queue-depth.json").read()
cmks = dict(l.rstrip("\n").split("\t", 1) for l in open("clawpool-cmks.pairs") if l.strip())
subs = {
    "__REGION__": env["region"],
    "__ACCOUNT_ID__": env["account"],
    "__DISPATCH_PARAM_PREFIX__": prefix,
    "__SSM_KMS_KEY_ARN__": kms_arn,
    "__CLAWPOOL_CMK_ARN__": cmks.get("CLAWPOOL_CMK_ARN"),
    "__CLAWPOOL_RSA_CMK_ARN__": cmks.get("CLAWPOOL_RSA_CMK_ARN"),
    "__DISPATCH_QUEUE_ARN__": arns.get("DISPATCH_QUEUE_URL"),
    "__DISPATCH_DLQ_ARN__": arns.get("DISPATCH_DLQ_URL"),
    "__LIFECYCLE_QUEUE_ARN__": arns.get("LIFECYCLE_QUEUE_URL"),
    "__LIFECYCLE_DLQ_ARN__": arns.get("LIFECYCLE_DLQ_URL"),
}
body = json.loads(doc)
# A CMK this deployment does not have means that feature is off: drop the statement outright
# rather than emitting a token or widening it to a wildcard.
for token, sid in [("__CLAWPOOL_CMK_ARN__", "DeviceIdentityEnvelope"),
                   ("__CLAWPOOL_RSA_CMK_ARN__", "AsymmetricV1PublicKeyRead")]:
    if not subs.get(token):
        body["Statement"] = [s for s in body["Statement"] if s["Sid"] != sid]
        print(f"dropped {sid}: this deployment sets no {token.strip('_')}")
        subs.pop(token)
doc = json.dumps(body, indent=2)
for token, value in subs.items():
    if token in doc and not value:
        raise SystemExit(f"refusing to emit a half-substituted policy: {token} has no value")
    doc = doc.replace(token, value)
if "__" in doc:
    raise SystemExit("refusing to emit a policy that still carries a token")
body = json.loads(doc)                        # must parse
assert not any("__" in json.dumps(s) for s in body["Statement"])
open("/tmp/oc-api-handler-extra.json", "w").write(doc)
print(doc)
PY
# snapshot what is there BEFORE adding anything
aws iam list-role-policies --region us-west-2 --role-name "$ROLE" \
  > "role-inline-before.$OC_RUN_ID.json"
aws iam list-attached-role-policies --region us-west-2 --role-name "$ROLE" \
  > "role-managed-before.$OC_RUN_ID.json"
# then, only if you decided to close the gap:
aws iam put-role-policy --region us-west-2 --role-name "$ROLE" \
  --policy-name OpenClawDispatchManifestAndQueueDepth \
  --policy-document file:///tmp/oc-api-handler-extra.json
```

Rollback is `aws iam delete-role-policy --role-name "$ROLE" --policy-name
OpenClawDispatchManifestAndQueueDepth`, and it is a true rollback only because the snapshot above
proves the inline policy did not exist before. One caution when you verify: an IAM change shows up
in `simulate-principal-policy` immediately but the data plane can lag it by minutes, so a call that
still fails right after the grant is not evidence the grant is wrong. The only judge is a real call
from the function.

`v-permissions-live-role-covers-every-call` in `manifest.json` runs exactly the simulator command
above and names each call site and the scope to add. It is a `B-lifecycle` check because it needs
the live role; it is read-only and it does not grant anything.

## #668 — the egress allow relaxation and the new dry-run endpoint

This kit carries bb's #668 increment. Two things change, and they are deliberately independent.

**The scope floor relaxes, the default does not.** `_ABSOLUTE_MIN_PREFIX` moves from `24` to `16` in
`egress_admin_service.py`, and `_ABSOLUTE_MIN_PREFIX_EXTRA_ALLOW` moves the same way in
`oc-egress-sim.py`. `_DEFAULT_MIN_PREFIX` stays `24`, so **with no environment variable set the
admitted scope is exactly what it was** — verified by running the shipped
`_extra_allow_min_prefix()`: no env yields `/24`, an explicit `EGRESS_EXTRA_ALLOW_MIN_PREFIX=16`
yields `/16`, and `EGRESS_EXTRA_ALLOW_MIN_PREFIX=8` is clamped back to `/16`. Widening is an operator
action, never a side effect of applying this kit.

Both files ship together on purpose. The host-side floor and the control-plane floor are asserted
equal by the internal test suite; shipping only the control-plane half would let the API admit a
`/16` that the host then refuses, so the rule set the ledger records would not be the rule set in
force. `v-668-host-and-controlplane-floors-agree` reads both constants out of the shipped bytes and
compares them.

**The new endpoint is `POST /hosts/egress/allow/validate`, and it is read-only.** It returns the
admission verdict for a rule set — including a machine-readable `criterion` per entry — without
applying anything, bounded to `_VALIDATE_MAX_ENTRIES = 64` per request.
`v-668-validate-is-read-only` proves the property structurally: it walks every validate-named
function in the shipped service and fails if any of them calls `put_parameter`, `put_item`,
`update_item`, `send_command`, `put_object` or any other mutation, and it also fails if the entry
bound is declared but never referenced.

**The route needs an API Gateway change, and that is the one step here you must review.** `#668`
adds two resources (`allow`, then `validate` beneath it) and one `POST` method. The operation is
`MANUAL_CLI_REVIEW` and driven by `lib/apply-api-routes.sh`:

```bash
bash lib/apply-api-routes.sh add --path /hosts/egress/allow/validate --method POST \
  --api-key-required --lambda "$OPENCLAW_API_FN" --rest-api-id "$REST_API_ID" --stage "$STAGE"
aws apigateway get-resources --rest-api-id "$REST_API_ID" --region us-west-2 \
  --query "items[?path=='/hosts/egress/allow/validate']" --output json
```

Two things about that step. A route that exists but has not been deployed answers **403 Missing
Authentication Token**, which reads like an authentication failure and is not one — it means the
stage has not been republished. And the Deployment republishes the **whole** stage, so it carries
every other pending resource change with it; look at what else is pending before you create it.
Rollback is the matching `lib/apply-api-routes.sh remove`.

No new permission is required for either half: the handler that serves the dry-run reads the same
config it already read, and the API key requirement is the existing `key_required` shape.

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

**2b. Host scripts (layer B-s3). The mode is not uniform, and getting it wrong took a fleet's
restore path down.** The rule is **how the control plane invokes the file**, not what layer it is
in. Two of these four are sent to `AWS-RunShellScript` as the **bare command path with no
interpreter prefix**, so they need the executable bit; the other two are invoked through an
explicit interpreter, which ignores it.

**The four targets are not under one directory.** `init-host.sh` is what actually builds a host, and
it splits them by extension: shell scripts land under `/home/ubuntu/`, Python lands under
`/opt/openclaw/`. Do not assume a common prefix — an earlier revision of this table put
`host-agent.py` under `/home/ubuntu/`, and every command in this step then pointed at a path that
does not exist on a real host.

| artifact | target | mode | why |
| --- | --- | --- | --- |
| `host-scripts/launch-vm.sh.patched` | `/home/ubuntu/launch-vm.sh` | **0755** | executed bare — `core/ssm_dispatch.py:71` and `:156` build the command as `f"/home/ubuntu/launch-vm.sh {tenant_id} …"`, and `services/tenant_service.py` does the same. `init-host.sh:855` |
| `host-scripts/stop-vm.sh.patched` | `/home/ubuntu/stop-vm.sh` | **0755** | executed bare from `services/dispatch_poller.py`. `init-host.sh:867` |
| `host-scripts/host-agent.py.patched` | **`/opt/openclaw/host-agent.py`** | 0644 | run by systemd as `/usr/bin/python3 /opt/openclaw/host-agent.py`; a long-lived service, restarted below. `init-host.sh:599` |
| `host-scripts/lib/harden-config.sh.patched` | `/home/ubuntu/lib/harden-config.sh` | 0644 | run as `bash lib/harden-config.sh`. `init-host.sh:861` |

**Confirm the path and the unit on the host before you write anything**, rather than trusting this
table. A wrong path fails loudly, which is survivable; a wrong **unit name** does not — the journal
check below would match nothing and read as clean:

```bash
systemctl show -p FragmentPath -p ExecStart -p MainPID --value host-agent.service
ls -l /opt/openclaw/host-agent.py /home/ubuntu/launch-vm.sh /home/ubuntu/stop-vm.sh
```

**`route_ops.py` is a same-directory dependency this kit does not ship.** `host-agent.py:27` does
`import route_ops` and `sys.path` gets `__file__`'s directory, so the two must live together in
`/opt/openclaw/` and must be version-compatible — `init-host.sh:601` says so explicitly. This kit
replaces `host-agent.py` and leaves `route_ops.py` at whatever the host already has. Before
restarting the service, check that the new `host-agent.py` needs nothing from `route_ops.py` that the
resident copy lacks; the journal check below is what catches it if it does, as an import error.

**Why this matters more than it looks.** S3 carries no unix permission bit, so the mode is
whatever the host-side install sets — the umask default is 0644. A previous run of a sibling kit
installed `launch-vm.sh` at 0644 on a 15-host fleet: every file was in place, every sha256 matched
the manifest, every assertion passed, and **every tenant restore failed** with
`restore_fail_reason=host_unreachable`, because the SSM invocation came back `rc=126`
`Permission denied`. Nothing in a hash check can see that. `init-host.sh` does `chmod +x` at boot,
so a *replacement* host self-heals and a hot-patched host does not — which is why the check below
is `test -x`, on the running host, rather than a comparison against the manifest.

Copying the file is not applying it: `host-agent.py` runs as a long-lived service, so the old
code stays resident until it restarts. `stop-vm.sh`, `launch-vm.sh` and `harden-config.sh` are
re-read per invocation and need no restart. Per host:

```bash
set -o pipefail
# Per-file paths, because the four targets are NOT under one directory.
for t in /opt/openclaw/host-agent.py /home/ubuntu/stop-vm.sh /home/ubuntu/launch-vm.sh \
         /home/ubuntu/lib/harden-config.sh; do
  test -f "$t" || { echo "FATAL $t does not exist on this host — confirm the real layout before writing"; exit 1; }
  sha256sum "$t"                       # compare each against the manifest patch_sha256
done
# The two bare-executed scripts must carry the bit. Assert it on the host, per host — a hash match
# says nothing about mode, and this is the check the fleet-wide restore outage would have failed.
for t in /home/ubuntu/launch-vm.sh /home/ubuntu/stop-vm.sh; do
  chmod 0755 "$t"
  test -x "$t" || { echo "FATAL $t is not executable: restore would return rc=126"; exit 1; }
  stat -c '%a %n' "$t"                 # must print 755
done
# Resolve the unit from the host, do not assume it. An empty MainPID means the name is wrong, and
# every journal check after that would match nothing and look clean.
UNIT=host-agent.service
systemctl show -p FragmentPath -p ExecStart --value "$UNIT" || { echo "FATAL unit $UNIT not found"; exit 1; }
systemctl restart "$UNIT"
systemctl is-active "$UNIT"             # must print active
pid=$(systemctl show -p MainPID --value "$UNIT")
test "${pid:-0}" -gt 0 || { echo "FATAL no MainPID for $UNIT — the unit name is wrong, not the service"; exit 1; }
# prove the RUNNING process is the new code, not just that the file on disk changed:
tr '\0' ' ' < "/proc/$pid/cmdline"; echo
sha256sum /opt/openclaw/host-agent.py
journalctl -u "$UNIT" --since '-2 min' --no-pager | tail -20   # no import error, no crash loop
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
package: `core/` holds 34 files and this kit ships 7 of them; `services/` holds 24 and the kit ships
6. Deleting either directory drops 45 files including `core/__init__.py` and `core/auth.py`, and the
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

Step 2 fixed the machines that are running. This step fixes what a machine launched *tomorrow*
downloads. They are separate channels and doing only the first leaves the fleet split: on a real
apply, 21 hot-fixed hosts ran the new `host-agent.py` while `deployment/scripts/host-agent.py` still
held the pre-patch bytes, so the next replacement host booted the old code and nothing reported a
failure.

### Step 3a — The host runtime scripts in `deployment/scripts/`

`init-host.sh` downloads these at boot from `s3://$ASSETS_BUCKET/deployment/scripts/`. This kit
replaces seven of them. There is no `apply-resource-ops.sh` operation for this prefix, so the
commands are here, and they carry the same anchor discipline the operations use: record the version
that is live *before* overwriting it, or a rollback has nothing to return to.

**Prerequisite — versioning.** Without it the pre-write bytes are unrecoverable, so this refuses to
run rather than take an unrollbackable write:

```bash
aws s3api get-bucket-versioning --bucket "$ASSETS_BUCKET" --region "$AWS_REGION" \
  --query Status --output text     # must print Enabled
```

**The seven objects and their kit artifacts.** Verify each artifact against `manifest.json`
`patch_sha256` first — uploading an artifact you have not hashed is how a mis-packaged kit reaches a
fleet:

| `deployment/scripts/` key | kit artifact |
| --- | --- |
| `host-agent.py` | `host-scripts/host-agent.py.patched` |
| `launch-vm.sh` | `host-scripts/launch-vm.sh.patched` |
| `stop-vm.sh` | `host-scripts/stop-vm.sh.patched` |
| `lib/harden-config.sh` | `host-scripts/lib/harden-config.sh.patched` |
| `lib/cred-inject.sh` | `host-scripts/lib/cred-inject.sh` |
| `route_ops.py` | `host-scripts/route_ops.py` |
| `oc-egress-sim.py` | `host-scripts/oc-egress-sim.py` |

```bash
set -o pipefail
test -n "$ASSETS_BUCKET" && test -n "$AWS_REGION" || { echo "FATAL set ASSETS_BUCKET and AWS_REGION"; exit 1; }
mkdir -p ./step3a
: > ./step3a/anchors.tsv

promote() {   # promote <key> <local artifact>
  local key="$1" src="$2"
  test -f "$src" || { echo "FATAL missing artifact $src"; return 1; }

  # 1. the anchor, BEFORE any write. ABSENT is a legitimate anchor (rollback = delete).
  local prev
  prev="$(aws s3api head-object --bucket "$ASSETS_BUCKET" --key "deployment/scripts/$key" \
            --region "$AWS_REGION" --query VersionId --output text 2>/dev/null || echo ABSENT)"
  test -n "$prev" || { echo "FATAL empty VersionId for $key — refusing to write blind"; return 1; }
  printf '%s\t%s\n' "$key" "$prev" >> ./step3a/anchors.tsv

  # 2. is it already current? Then skip it — this makes a re-run of the whole kit a no-op.
  local want live
  want="$(sha256sum "$src" | cut -d' ' -f1)"
  live="$(aws s3api get-object --bucket "$ASSETS_BUCKET" --key "deployment/scripts/$key" \
            --region "$AWS_REGION" /dev/stdout 2>/dev/null | sha256sum | cut -d' ' -f1)"
  if [ "$live" = "$want" ]; then echo "skip  $key already at $want"; return 0; fi

  # 3. stage to a temp key and prove the upload arrived intact before touching the real key.
  aws s3 cp "$src" "s3://$ASSETS_BUCKET/deployment/scripts/.staging/$key" \
    --region "$AWS_REGION" --only-show-errors
  local staged
  staged="$(aws s3api get-object --bucket "$ASSETS_BUCKET" \
              --key "deployment/scripts/.staging/$key" --region "$AWS_REGION" /dev/stdout \
            | sha256sum | cut -d' ' -f1)"
  test "$staged" = "$want" || { echo "FATAL staged copy of $key hashes $staged, want $want"; return 1; }

  # 4. promote, then read the REAL key back. A copy that reported success is not a copy that landed.
  aws s3api copy-object --bucket "$ASSETS_BUCKET" --key "deployment/scripts/$key" \
    --copy-source "$ASSETS_BUCKET/deployment/scripts/.staging/$key" \
    --region "$AWS_REGION" --output text --query CopyObjectResult.ETag > /dev/null
  local after
  after="$(aws s3api get-object --bucket "$ASSETS_BUCKET" --key "deployment/scripts/$key" \
             --region "$AWS_REGION" /dev/stdout | sha256sum | cut -d' ' -f1)"
  test "$after" = "$want" || { echo "FATAL $key reads back $after, want $want"; return 1; }
  echo "ok    $key -> $want (rollback anchor $prev)"
}

promote host-agent.py            host-scripts/host-agent.py.patched
promote launch-vm.sh             host-scripts/launch-vm.sh.patched
promote stop-vm.sh               host-scripts/stop-vm.sh.patched
promote lib/harden-config.sh     host-scripts/lib/harden-config.sh.patched
promote lib/cred-inject.sh       host-scripts/lib/cred-inject.sh
promote route_ops.py             host-scripts/route_ops.py
promote oc-egress-sim.py         host-scripts/oc-egress-sim.py

aws s3 rm "s3://$ASSETS_BUCKET/deployment/scripts/.staging/" --recursive \
  --region "$AWS_REGION" --only-show-errors
cat ./step3a/anchors.tsv
```

**The executable bit is not part of this step, and that is correct.** S3 stores no unix mode, so the
bit cannot travel with the object. `init-host.sh:857` and `:869` `chmod +x` `launch-vm.sh` and
`stop-vm.sh` after downloading them, and `:866` does the same for `lib/harden-config.sh` and
`lib/cred-inject.sh` — a future host sets its own modes. The manual `chmod 0755` in Step 2b exists
only because hot-copying onto a *running* host bypasses that code path.

**Verify — as a new machine, not as a bucket read.** Reading the key back proves the upload; it does
not prove a booting host consumes it. Launch one replacement host and check it against a sibling:

```bash
# On a NEWLY launched host (after it registers in openclaw-hosts):
sha256sum /opt/openclaw/host-agent.py /home/ubuntu/launch-vm.sh /home/ubuntu/stop-vm.sh \
          /home/ubuntu/lib/harden-config.sh /home/ubuntu/lib/cred-inject.sh \
          /opt/openclaw/route_ops.py
# Every value must equal this kit's patch_sha256 for that path AND equal what a Step-2b host reports.
# A difference between a new host and a hot-fixed host is this step having silently not taken effect.
```

**Rollback.** Per key, restore the exact version recorded in `./step3a/anchors.tsv`:

```bash
while IFS=$'\t' read -r key prev; do
  if [ "$prev" = "ABSENT" ]; then
    aws s3api delete-object --bucket "$ASSETS_BUCKET" --key "deployment/scripts/$key" \
      --region "$AWS_REGION"
  else
    aws s3api copy-object --bucket "$ASSETS_BUCKET" --key "deployment/scripts/$key" \
      --copy-source "$ASSETS_BUCKET/deployment/scripts/$key?versionId=$prev" \
      --region "$AWS_REGION" --output text --query CopyObjectResult.ETag > /dev/null
  fi
  echo "restored $key to $prev"
done < ./step3a/anchors.tsv
```

Rolling this back alone re-splits the fleet in the other direction: future hosts return to the old
code while the Step 2b hosts keep the new. Roll Step 2b back too, or accept the split knowingly.

### Step 3c — The edge Launch Template

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

`manifest.json` `verifications[]` carries all 52 checks with exact `action`, `observable`,
`pass_when` and `fail_when`: 30 read-only, 17 lifecycle, 5 optional. Run every read-only check.

**Every check is read-only.** None writes to the product. The ones that reach a host do it with
`ssm send-command` running `iptables -S`, a metrics `curl`, or `journalctl | grep` — reads, and
the SSM invocation record is unavoidable because there is no read-only API for host inspection.
The checks that import a shipped module set `sys.dont_write_bytecode`, so running them leaves no
`__pycache__` inside the kit — which matters, because a stray one makes the apply driver report
a file the manifest never declared. Several checks write scratch under `/tmp`; nothing in the
kit, the fleet or the control plane is modified.

Measured off-machine on this kit: **28 of the 30 read-only checks pass with no AWS credentials
and no live environment**, and the 17 lifecycle checks are the ones that need the environment —
they are labelled that way precisely so a red without it is read as "the environment is absent",
not "the assertion failed".

### The one read-only check that is red on purpose

`v-636-openapi-covers-live-egress` fails, and it is telling the truth rather than misfiring:

```
declared ['/hosts/egress', '/hosts/egress/chain', '/hosts/egress/revisions',
          '/hosts/egress/rollback']
missing  ['/hosts/egress/allow', '/hosts/egress/revoke', '/hosts/egress/convergence',
          '/hosts/egress/rollout', '/hosts/egress/fleet']
```

`docs/aws-guide/openapi.yaml` documents four egress paths plus the new
`/hosts/egress/allow/validate`, while the service answers five more. That is a documentation gap
in the published spec, not a defect this kit introduces or can close: writing the spec is #636's
own subject. It is left red rather than relaxed, because a check quietly widened to accept the gap
would then never notice the next missing route. Treat those five as undocumented-but-live when
you integrate against the API.

Two of the new checks are worth reading before you run them. `v-657-partial-collection-attribution`
is a **source-level pin**, not a live call: it asserts the shipped `fleet_egress` body carries the
207 decision, the three attribution fields and the `targets="all"` exemption. It cannot prove the
endpoint's runtime answer, and its `fail_when` says so. `v-658-placeholder-count-is-repeatable` runs
the shipped counter 40 times on one unchanged `nginx.conf` and asserts the **set** of verdicts has a
single element — a single run cannot observe the flakiness that check exists for.

Four of the new checks are worth reading before you run them.

`v-657-ssm-timeout-lower-bound` compiles **only** `_dispatch_apply` out of the shipped bytes and
calls it with a recording stub in place of `clients.ssm`, so the six `EGRESS_APPLY_TIMEOUT` values
are read out of the function body rather than recomputed by the check. Importing the module is not
an option and must not be made one: the kit ships 7 of `core/`'s 34 files by design, so the import
chain would reach modules it correctly does not carry.

`v-657-partial-collection-attribution` does the same for `fleet_egress` and drives three collection
outcomes: a targeted call that came back short must answer **207** with the missing id named, the
same call complete must answer **200**, and `targets="all"` with a DDB snapshot larger than what
answered must still answer 200 — a set difference there would manufacture a permanent 207.

`v-657-deployed-package-carries-the-clamp` downloads the package the live function is actually
running and asserts its `services/egress_admin_service.py` is **byte-identical** to the artifact this
kit ships, which is what carries the measured six-value result over to the deployment. It does not
prove the endpoint's response for an `EGRESS_APPLY_TIMEOUT` at or below 19; inducing that would mean
setting an illegal timeout on a running control plane.

`v-658-block-boundary-is-brace-paired` covers #658's **second** root cause. The repeatability check
alone passes even with the old indentation-guessed boundary restored, so this one asserts the counter
contains no pipe at all and still counts 4 on a fixture whose nested closing brace sits at exactly
four spaces.

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

## Step 8 — The pre-launch validator, and it is the last thing you run

`patch/validator/` is generic — it belongs to no kit, and every patch ends here. It answers a
question none of the earlier steps can: **does the environment now actually match what this kit
promised, and is there anything in the repository that is green everywhere and still broken?**

It is read-only. It never invokes the function, never writes a parameter, never touches the fleet.
The host readings it takes go through `AWS-RunShellScript` with `sha256sum` / `stat` /
`systemctl is-active` / `journalctl` payloads only.

```bash
patch/validator/oc-prelaunch-validate \
  --kit patch/lc2-patch \
  --environment-json ./environment.json \
  --region "$AWS_REGION" \
  --target-vms 100000 \
  --report "prelaunch-$OC_RUN_ID.json"
```

Exit codes are three-valued on purpose: `0` everything passed, `1` at least one FAIL, `2`
INCONCLUSIVE with no FAIL. `2` is not a pass — it means a check could not see what it needed, and
"could not see" and "saw something wrong" call for different next actions. Run it with `--offline`
first if you have no credentials yet; that subset needs none.

**Read the readings, not just the verdicts.** Every finding prints the values it actually observed.
Three things to expect on a first run:

- A `DIVERGED` env row is not automatically a defect. The baseline is the default declared in the
  gateway source, and a deliberate tuning shows up as divergence. The tool states both values and
  leaves the judgement to you.
- `UNVERIFIED` on a mode or hash check usually means the live half was unreachable, not that the
  declared half is wrong.
- The known-false-red list in `patch/validator/README.md` exists so a red there does not trigger a
  rollback. `apply-api-routes verify`'s CORS FATAL, `oc-consistency`'s handful of DRIFT rows, the
  `{{ }}` gate matching a comment, and a `grep -c` that exits 1 on zero matches have each caused a
  wasted round or a near-miss rollback before.

What it caught on its own first run against this kit, which is the reason it exists: `launch-vm.sh`
and `stop-vm.sh` are sent to `AWS-RunShellScript` as bare command paths, so they need mode 0755 —
and this document previously told you to install every host script at 0644. Every hash matched,
every assertion passed, and a fleet's tenant restore would have returned `rc=126`.
