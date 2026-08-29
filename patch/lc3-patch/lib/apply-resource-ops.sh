#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apply-resource-ops.sh — the one executable path for every CloudFormation resource this kit
# owns. No stack update is involved: each operation is the by-hand AWS CLI equivalent, wrapped
# so that every executor (human or agent) runs the SAME code instead of retyping a command.
#
#   lib/apply-resource-ops.sh <op> <apply|verify|rollback|plan>
#
# Every op reads its coordinates from environment.json (produced by lib/discover-env.sh in
# APPLY Step 0). Nothing here guesses a name: a coordinate that is absent is a hard stop, not
# a default, because the failure mode of a guessed function or role name is "the command
# succeeded against the wrong resource".
#
# State (backups, pre-state snapshots) lands in ./.lc3-state/<op>/ next to the kit. rollback
# refuses to run when that state is missing — a rollback that invents its own target is worse
# than no rollback.
set -euo pipefail

OP="${1:?op name required}"
MODE="${2:?mode required: apply|verify|rollback|plan}"
case "$MODE" in
apply | verify | rollback | plan) ;;
*)
  echo "unknown mode: $MODE (want apply|verify|rollback|plan)" >&2
  exit 2
  ;;
esac

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_JSON="${OC_ENV_JSON:-$KIT_DIR/environment.json}"
STATE_DIR="$KIT_DIR/.lc3-state/$OP"
PARAM_NAME="/openclaw/lifecycle/fence-lease-sec"
PARAM_DEFAULT=240
PARAM_FLOOR=210

die() {
  echo "STOP: $*" >&2
  exit 1
}

note() { echo "  $*"; }

# ---------------------------------------------------------------- coordinates
# Read one coordinate out of environment.json. Absent or empty is fatal for every field the
# caller asks for, so a partially-probed environment cannot half-apply an operation.
env_field() {
  local key="$1" optional="${2:-}"
  [ -f "$ENV_JSON" ] || die "environment.json not found at $ENV_JSON — run lib/discover-env.sh first"
  local value
  value="$(
    OC_KEY="$key" python3 - "$ENV_JSON" <<'PY'
import json, os, sys
key = os.environ["OC_KEY"]
try:
    data = json.load(open(sys.argv[1]))
except Exception as exc:
    print("", end="")
    sys.exit(0)
node = data
for part in key.split("."):
    if isinstance(node, dict) and part in node:
        node = node[part]
    else:
        node = None
        break
if node is None or isinstance(node, (dict, list)):
    print("", end="")
else:
    print(node, end="")
PY
  )"
  if [ -z "$value" ]; then
    [ -n "$optional" ] && return 0
    die "environment.json has no usable value for '$key' — resolve it before applying $OP"
  fi
  printf '%s' "$value"
}

region() { env_field region; }
account() { env_field account; }

param_arn() { printf 'arn:aws:ssm:%s:%s:parameter%s' "$(region)" "$(account)" "$PARAM_NAME"; }

# ------------------------------------------------------------------ IAM grant
# Shared by the two permission ops. Both are read-only grants and both are RETAIN: an
# equivalent grant already present means skip rather than duplicate, and removing one can
# break rolled-back code that still reads the resource.
iam_grant() {
  local mode="$1" role="$2" policy_name="$3" doc="$4" action="$5" resource="$6"
  local role_arn="arn:aws:iam::$(account):role/$role"
  case "$mode" in
  plan)
    note "role=$role policy=$policy_name action=$action resource=$resource"
    ;;
  apply)
    mkdir -p "$STATE_DIR"
    aws iam get-role-policy --role-name "$role" --policy-name "$policy_name" \
      --output json >"$STATE_DIR/pre-$policy_name.json" 2>/dev/null ||
      echo '{"absent":true}' >"$STATE_DIR/pre-$policy_name.json"
    local decision
    decision="$(aws iam simulate-principal-policy --policy-source-arn "$role_arn" \
      --action-names "$action" --resource-arns "$resource" \
      --query 'EvaluationResults[0].EvalDecision' --output text)"
    if [ "$decision" = "allowed" ]; then
      note "already allowed for $role — skipping (an equivalent grant exists; do not duplicate)"
      return 0
    fi
    note "adding $policy_name to $role"
    aws iam put-role-policy --role-name "$role" --policy-name "$policy_name" \
      --policy-document "file://$doc"
    ;;
  verify)
    local decision
    decision="$(aws iam simulate-principal-policy --policy-source-arn "$role_arn" \
      --action-names "$action" --resource-arns "$resource" \
      --query 'EvaluationResults[0].EvalDecision' --output text)"
    # The IAM data plane can lag the simulator by minutes. A single allowed reading is the
    # floor, not proof; the per-fix check in APPLY Step 6 is what confirms real behaviour.
    [ "$decision" = "allowed" ] || die "$role is still $decision for $action on $resource"
    note "allowed: $role -> $action"
    ;;
  rollback)
    note "RETAIN by policy — $policy_name on $role is read-only and rolled-back code still"
    note "reads the resource. Removing it converts a working fallback into AccessDenied."
    note "To remove deliberately: aws iam delete-role-policy --role-name $role --policy-name $policy_name"
    ;;
  esac
}

# --------------------------------------------------------------- Lambda code
# Per-file overlay: the customer's own package supplies the dependencies, so no dependency
# version is silently replaced. The zip is built by APPLY Step 2; this op owns the update,
# the readiness wait, the invoke check and the alias move.
lambda_code() {
  local mode="$1" fn_key="$2" src="$3" has_alias="$4"
  local fn
  fn="$(env_field "$fn_key")"
  local zip="$KIT_DIR/lc3-$fn.zip"
  case "$mode" in
  plan)
    note "function=$fn source=$src alias=${has_alias:-none} zip=$zip"
    ;;
  apply)
    mkdir -p "$STATE_DIR"
    [ -f "$zip" ] || die "$zip missing — build it in APPLY Step 2 before this op"
    aws lambda get-function --function-name "$fn" \
      --query 'Configuration.[RevisionId,CodeSha256,Version]' --output text \
      >"$STATE_DIR/pre-$fn.txt"
    local url
    url="$(aws lambda get-function --function-name "$fn" --query Code.Location --output text)"
    curl -sS "$url" -o "$STATE_DIR/backup-$fn.zip"
    [ -s "$STATE_DIR/backup-$fn.zip" ] || die "backup zip for $fn is empty — refusing to apply"
    aws lambda publish-version --function-name "$fn" \
      --description "pre-lc3-patch anchor" --query Version --output text \
      >"$STATE_DIR/anchor-$fn.txt"
    if [ -n "$has_alias" ]; then
      aws lambda get-alias --function-name "$fn" --name "$has_alias" \
        --query FunctionVersion --output text >"$STATE_DIR/alias-$fn.txt"
    fi
    note "backed up $fn (revision + bytes + version anchor)"
    aws lambda update-function-code --function-name "$fn" --zip-file "fileb://$zip" \
      --query '[LastUpdateStatus,CodeSha256]' --output text
    aws lambda wait function-updated --function-name "$fn"
    # Judge on FunctionError, not on a 200 body: on a private API a synthetic path returns
    # 404 by routing, which is expected and is not a failure of the code update.
    local err
    err="$(aws lambda invoke --function-name "$fn" \
      --payload '{"httpMethod":"GET","path":"/ping"}' --cli-binary-format raw-in-base64-out \
      /dev/null --query FunctionError --output text)"
    [ "$err" = "None" ] || die "$fn returned FunctionError=$err after the code update"
    if [ -n "$has_alias" ]; then
      local newver
      newver="$(aws lambda publish-version --function-name "$fn" --query Version --output text)"
      aws lambda update-alias --function-name "$fn" --name "$has_alias" \
        --function-version "$newver" >/dev/null
      note "alias $has_alias -> version $newver"
    fi
    ;;
  verify)
    local before now
    before="$(cut -f2 "$STATE_DIR/pre-$fn.txt" 2>/dev/null || true)"
    [ -n "$before" ] || die "no pre-state for $fn — cannot judge the update without a baseline"
    now="$(aws lambda get-function --function-name "$fn" \
      --query Configuration.CodeSha256 --output text)"
    [ "$now" != "$before" ] || die "$fn CodeSha256 is unchanged ($now) — the update did not land"
    note "CodeSha256 changed: $before -> $now"
    ;;
  rollback)
    [ -s "$STATE_DIR/backup-$fn.zip" ] ||
      die "no backup zip for $fn in $STATE_DIR — refusing to roll back to an invented target"
    # Both halves. An SQS event source mapping binds $LATEST, so moving the alias alone
    # leaves the queue consumer path running the new code.
    if [ -n "$has_alias" ] && [ -s "$STATE_DIR/alias-$fn.txt" ]; then
      aws lambda update-alias --function-name "$fn" --name "$has_alias" \
        --function-version "$(cat "$STATE_DIR/alias-$fn.txt")" >/dev/null
      note "alias $has_alias restored to $(cat "$STATE_DIR/alias-$fn.txt")"
    fi
    aws lambda update-function-code --function-name "$fn" \
      --zip-file "fileb://$STATE_DIR/backup-$fn.zip" --query LastUpdateStatus --output text
    aws lambda wait function-updated --function-name "$fn"
    note "\$LATEST restored from backup bytes"
    ;;
  esac
}

# ----------------------------------------------------------------------- ops
case "$OP" in
ssm-fence-lease-param)
  case "$MODE" in
  plan) note "parameter=$PARAM_NAME default=$PARAM_DEFAULT floor=$PARAM_FLOOR" ;;
  apply)
    mkdir -p "$STATE_DIR"
    if aws ssm get-parameter --name "$PARAM_NAME" --query Parameter.Value --output text \
      >"$STATE_DIR/before.txt" 2>/dev/null; then
      note "parameter already exists with value $(cat "$STATE_DIR/before.txt") — adopting it"
      note "(a re-run is a no-op; raising the value is a deliberate, separate decision)"
    else
      : >"$STATE_DIR/before.txt"
      aws ssm put-parameter --name "$PARAM_NAME" --type String --value "$PARAM_DEFAULT" \
        --description "openclaw lifecycle fence lease in seconds. Takes effect immediately with no stack update. Read by the api and lifecycle-consumer functions with a 60s in-process cache. Floor is 210s, the longest fenced action's execution budget." \
        --query Version --output text
      note "created $PARAM_NAME = $PARAM_DEFAULT"
    fi
    ;;
  verify)
    value="$(aws ssm get-parameter --name "$PARAM_NAME" --query Parameter.Value --output text)"
    case "$value" in
    '' | *[!0-9]*) die "$PARAM_NAME is not an integer: '$value'" ;;
    esac
    [ "$value" -ge "$PARAM_FLOOR" ] ||
      die "$PARAM_NAME=$value is below the floor $PARAM_FLOOR — the runtime rejects it and falls back to $PARAM_DEFAULT, so this value silently does not apply"
    note "$PARAM_NAME = $value (>= $PARAM_FLOOR)"
    ;;
  rollback)
    [ -f "$STATE_DIR/before.txt" ] || die "no pre-state — refusing to guess the previous value"
    if [ -s "$STATE_DIR/before.txt" ]; then
      aws ssm put-parameter --overwrite --name "$PARAM_NAME" --type String \
        --value "$(cat "$STATE_DIR/before.txt")" --query Version --output text
      note "restored $PARAM_NAME to $(cat "$STATE_DIR/before.txt")"
    else
      aws ssm delete-parameter --name "$PARAM_NAME"
      note "deleted $PARAM_NAME (this op created it; the code falls back to $PARAM_DEFAULT)"
    fi
    ;;
  esac
  ;;

iam-api-fence-param-read)
  # The grant lands in the API role's OVERFLOW managed policy when synthesized, and that
  # split also relocates a pre-existing statement between overflow documents. On a live
  # deployment a small inline policy is the equivalent, and nothing already present is moved
  # or removed. If this deployment also runs a lifecycle consumer, set CONSUMER_ROLE in
  # environment.json: the consumer is the primary executor of async lifecycle actions, and a
  # grant present on the API side only makes the two diverge the moment the value is edited.
  iam_grant "$MODE" "$(env_field api_role)" lc3-lifecycle-fence-lease-param-read \
    "$KIT_DIR/iam/lifecycle-fence-lease-param-read.json" ssm:GetParameter "$(param_arn)"
  consumer="$(env_field consumer_role optional || true)"
  if [ -n "${consumer:-}" ]; then
    iam_grant "$MODE" "$consumer" lc3-lifecycle-fence-lease-param-read \
      "$KIT_DIR/iam/lifecycle-fence-lease-param-read.json" ssm:GetParameter "$(param_arn)"
  else
    note "no consumer_role in environment.json — if this deployment runs a lifecycle consumer,"
    note "add it and re-run, or the api and consumer will read different lease values."
  fi
  ;;

iam-health-describe-instances)
  # ec2:Describe* has no resource-level IAM, so the resource is "*" and the statement is
  # read-only. Without it the reaper cannot confirm host death against EC2 and falls back to
  # the hosts-table clue, which can mark a tenant whose VM is still running as terminal.
  iam_grant "$MODE" "$(env_field health_role)" lc3-health-describe-instances \
    "$KIT_DIR/iam/health-describe-instances.json" ec2:DescribeInstances '*'
  ;;

lambda-api-code)
  lambda_code "$MODE" api_function lambda/api live
  consumer_fn="$(env_field consumer_function optional || true)"
  if [ -n "${consumer_fn:-}" ]; then
    note "consumer function present — the same seven api files must be overlaid onto it"
    OC_ENV_JSON="$ENV_JSON" lambda_code "$MODE" consumer_function lambda/api ""
  else
    note "no consumer_function in environment.json — confirm this deployment has no queue"
    note "consumer before treating the api-only update as complete."
  fi
  ;;

lambda-health-code)
  lambda_code "$MODE" health_function lambda/health_check ""
  ;;

codebuild-goldenimage-asset-drift)
  # The golden-image builder's source asset key moves because the builder packages the
  # repository tree and this range changed files inside it. The ONLY difference is that
  # object key. Nothing on the running system depends on it and this kit uploads no such
  # asset, so the correct action now is to change nothing and record the current value, so a
  # later image bake can be attributed. This is why the op requires operator review.
  project="$(env_field golden_image_project optional || true)"
  case "$MODE" in
  plan | apply)
    mkdir -p "$STATE_DIR"
    if [ -z "${project:-}" ]; then
      note "no golden_image_project in environment.json — nothing to record; this op is a"
      note "no-op on the running system either way."
      exit 0
    fi
    aws codebuild batch-get-projects --names "$project" \
      --query 'projects[0].source.location' --output text | tee "$STATE_DIR/source-location.txt"
    note "recorded only. This kit changes nothing here; the new asset key comes into"
    note "existence at the next image bake, which must take its source from the patched tree."
    ;;
  verify)
    [ -n "${project:-}" ] || {
      note "no golden_image_project — nothing to verify"
      exit 0
    }
    now="$(aws codebuild batch-get-projects --names "$project" \
      --query 'projects[0].source.location' --output text)"
    if [ -f "$STATE_DIR/source-location.txt" ]; then
      [ "$now" = "$(cat "$STATE_DIR/source-location.txt")" ] ||
        die "the builder source moved from $(cat "$STATE_DIR/source-location.txt") to $now — this kit does not change it, so something else did"
    fi
    note "builder source unchanged by this kit: $now"
    ;;
  rollback)
    note "RETAIN — nothing was changed, so there is nothing to roll back."
    ;;
  esac
  ;;

apigw-identify-live)
  # READ-ONLY. Pick the control-plane API by OBSERVED TRAFFIC, never by name, then read its
  # resource policy and print what a call that policy would actually admit looks like.
  #
  # Why traffic and not a name: an account commonly holds several REST APIs with plausible
  # names (an old one, a redeployed one, a copy from a rehearsal). A name match that picks the
  # wrong one makes every later probe test the wrong system and read as "the fix is not live".
  # A 403 is equally ambiguous on its own -- wrong API, wrong auth, or a resource policy that
  # excludes your source -- which is why this op prints the policy instead of guessing.
  case "$MODE" in
  plan | verify)
    mkdir -p "$STATE_DIR"
    end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    start="$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
    echo "REST APIs in this account/region, ranked by request count over the last 7 days:"
    aws apigateway get-rest-apis --query 'items[].[id,name]' --output text |
      while read -r api_id api_name; do
        [ -n "$api_id" ] || continue
        count="$(aws cloudwatch get-metric-statistics --namespace AWS/ApiGateway \
          --metric-name Count --dimensions "Name=ApiName,Value=$api_name" \
          --start-time "$start" --end-time "$end" --period 604800 --statistics Sum \
          --query 'Datapoints[0].Sum' --output text 2>/dev/null)"
        [ "$count" = "None" ] || [ -z "$count" ] && count=0
        printf '  requests=%-12s id=%s  name=%s\n' "$count" "$api_id" "$api_name"
      done | sort -t= -k2 -rn | tee "$STATE_DIR/api-traffic.txt"
    echo
    echo "A zero-request API is not necessarily wrong (a private control plane can be idle),"
    echo "but an API with traffic that you were NOT going to target is a red flag -- reconcile"
    echo "before continuing."
    echo
    configured="$(env_field api_id optional || true)"
    if [ -z "${configured:-}" ]; then
      echo "environment.json carries no api_id. Take the URL your deployed client configuration"
      echo "actually uses (PRIVATE_API_URL / CTRL_API_BASE), extract its api id, and record it"
      echo "as api_id -- do not adopt the top row of the table above on its own."
      exit 0
    fi
    echo "Resource policy of the configured API ($configured):"
    aws apigateway get-rest-api --rest-api-id "$configured" \
      --query 'policy' --output text | tee "$STATE_DIR/resource-policy.raw" |
      python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw or raw == "None":
    print("  NO resource policy. For a private API that means the VPC endpoint policy and IAM")
    print("  are the only controls; for a regional/edge API it means the API is open to anyone")
    print("  who can reach it and holds a valid key.")
    raise SystemExit(0)
try:
    pol = json.loads(raw.replace("\\\\", ""))
except Exception as exc:
    print("  could not parse the policy (%s); printed raw above" % exc)
    raise SystemExit(0)
for st in pol.get("Statement", []):
    eff = st.get("Effect")
    cond = st.get("Condition") or {}
    print("  %s %s" % (eff, st.get("Action")))
    for op, kv in cond.items():
        for key, val in kv.items():
            print("      %s %s = %s" % (op, key, val))
print()
print("  Build the probe call from those conditions, in this order:")
print("   1. aws:SourceVpce  -> you must call from inside that VPC endpoint. curl from a")
print("      bastion in the VPC; a call from your laptop returns 403 no matter what auth you")
print("      hold, and that 403 says nothing about the fix.")
print("   2. aws:SourceIp    -> the call must originate from that range.")
print("   3. aws:PrincipalArn / execute-api:* -> sign with SigV4 as that principal.")
print("   4. If the API also requires an API key, send x-api-key as well; a missing key is a")
print("      403 that looks identical to a policy denial.")
'
    echo
    echo "SigV4 (AWS_IAM) methods — how to sign, on THIS tree:"
    bff="$KIT_DIR/../../deploy/console-bff/sigv4-client.mjs"
    if [ -f "$bff" ]; then
      echo "  The console BFF signs control-plane calls with SignatureV4 imported from"
      grep -m1 'from "@' "$bff" | sed 's/^/    /'
      echo "  and its own header says the module is NOT bundled: on nodejs20.x the runtime"
      echo "  already provides it (verified on a real function, node v20.20.2). So the check is"
      echo "  'does an import succeed on the target runtime', not 'is a package installed here':"
      echo
      echo "    aws lambda invoke --function-name <the bff function> \\"
      echo "      --payload '{\"probe\":\"sigv4\"}' /dev/null --query FunctionError   # want None"
      echo
      echo "  If you are signing by hand from a shell instead, use the CLI's own signer:"
      echo "    awscurl / aws --cli-binary-format, or 'aws apigateway test-invoke-method' for a"
      echo "    read-only method — do not hand-roll a signature."
    else
      echo "  deploy/console-bff/sigv4-client.mjs is not in this checkout; skip this block."
    fi
    echo
    echo "  Three things do NOT hold on the public gateway tree, so do not run them as a gate:"
    echo "   - deploy/console-bff has no package.json, so 'npm ci --omit=dev' has nothing to do."
    echo "   - the import is @aws-sdk/signature-v4, NOT @smithy/signature-v4, so a"
    echo "     'test -d node_modules/@smithy/signature-v4' gate fails on a healthy tree."
    echo "   - deploy/cdk-cli does not exist here, so a pinned-cdk-version gate cannot run."
    echo "     This kit never updates a stack, so the CDK version is not on its critical path;"
    echo "     if you need the synth provenance it is already bound in"
    echo "     resources/cloudformation/*.assembly-index.json."
    ;;
  apply | rollback)
    note "read-only op — nothing to apply or roll back. Use: plan (or verify)."
    ;;
  esac
  ;;

lambda-env-spread-and-floor)
  # Lower the ssm:SendCommand call rate: SPREAD_MAX_HOSTS_PER_BATCH 6 -> 3 and
  # HOST_SELECTION_SCORE_FLOOR 0.5 -> 0.25 on BOTH the api function and the lifecycle
  # consumer. Neither knob is injected by the stack today, so the effective value is the code
  # default in core/clients.py; this op sets it as an environment variable, which those two
  # lines already honour, so no code changes.
  #
  # TWO THINGS THAT SILENTLY LOSE THIS CHANGE:
  #
  #  1. Replacing the whole Variables map. update-function-configuration --environment takes a
  #     COMPLETE map and replaces it; passing only these two keys deletes every other variable
  #     the function has (dozens: table names, bucket names, deadline knobs). This op reads the
  #     live map back and merges, and refuses to write if the readback looks empty.
  #  2. Forgetting that the api function serves traffic through its `live` alias, i.e. through
  #     a PUBLISHED VERSION whose environment is a frozen snapshot. update-function-configuration
  #     only touches $LATEST, which serves nothing. So on the api side this op publishes a
  #     version and moves the alias; on the consumer side it does not, because the SQS event
  #     source mapping binds $LATEST and the new value is live the moment it is written.
  #     Doing only one of the two functions leaves api and consumer on different values.
  KEY1=SPREAD_MAX_HOSTS_PER_BATCH
  VAL1=3
  KEY2=HOST_SELECTION_SCORE_FLOOR
  VAL2=0.25

  env_set_one() {
    local fn="$1" alias_name="$2"
    mkdir -p "$STATE_DIR"
    local before="$STATE_DIR/env-before-$fn.json"
    aws lambda get-function-configuration --function-name "$fn" \
      --query Environment.Variables --output json >"$before"
    local n
    n="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1])) or {}))' "$before")"
    [ "$n" -gt 0 ] ||
      die "read back 0 variables for $fn — writing now would wipe its environment; investigate before retrying"
    note "$fn currently has $n variable(s); merging 2 keys into that map (never replacing it)"
    local merged="$STATE_DIR/env-merged-$fn.json"
    OC_K1="$KEY1" OC_V1="$VAL1" OC_K2="$KEY2" OC_V2="$VAL2" \
      python3 - "$before" "$merged" <<'PY'
import json, os, sys
cur = json.load(open(sys.argv[1])) or {}
cur[os.environ["OC_K1"]] = os.environ["OC_V1"]
cur[os.environ["OC_K2"]] = os.environ["OC_V2"]
json.dump({"Variables": cur}, open(sys.argv[2], "w"), indent=1, sort_keys=True)
print("  merged map has %d variable(s)" % len(cur))
PY
    aws lambda update-function-configuration --function-name "$fn" \
      --environment "file://$merged" --query 'Environment.Variables.[SPREAD_MAX_HOSTS_PER_BATCH,HOST_SELECTION_SCORE_FLOOR]' \
      --output text
    aws lambda wait function-updated --function-name "$fn"
    if [ -n "$alias_name" ]; then
      aws lambda get-alias --function-name "$fn" --name "$alias_name" \
        --query FunctionVersion --output text >"$STATE_DIR/alias-before-$fn.txt"
      local newver
      newver="$(aws lambda publish-version --function-name "$fn" --query Version --output text)"
      aws lambda update-alias --function-name "$fn" --name "$alias_name" \
        --function-version "$newver" >/dev/null
      note "$fn: published version $newver and moved alias $alias_name onto it"
      note "(without this the api keeps serving the frozen env of the previous version)"
    else
      note "$fn: no alias — the event source mapping binds \$LATEST, so this is already live"
    fi
  }

  env_verify_one() {
    local fn="$1" alias_name="$2" target="$fn"
    # Read the version that actually serves, not $LATEST.
    if [ -n "$alias_name" ]; then
      local ver
      ver="$(aws lambda get-alias --function-name "$fn" --name "$alias_name" \
        --query FunctionVersion --output text)"
      target="$fn:$ver"
      note "$fn serves version $ver through alias $alias_name — reading THAT version's env"
    fi
    local got1 got2
    got1="$(aws lambda get-function-configuration --function-name "$target" \
      --query "Environment.Variables.$KEY1" --output text)"
    got2="$(aws lambda get-function-configuration --function-name "$target" \
      --query "Environment.Variables.$KEY2" --output text)"
    [ "$got1" = "$VAL1" ] || die "$target has $KEY1=$got1, want $VAL1 (None means the served version predates the change)"
    [ "$got2" = "$VAL2" ] || die "$target has $KEY2=$got2, want $VAL2"
    note "$target: $KEY1=$got1 $KEY2=$got2"
  }

  # The two function names are fixed in this product, so they are defaults rather than probes.
  # environment.json may override them if a deployment renamed either function.
  api_fn="$(env_field api_function optional || true)"
  api_fn="${api_fn:-openclaw-api}"
  consumer_fn="$(env_field consumer_function optional || true)"
  consumer_fn="${consumer_fn:-openclaw-lifecycle-consumer}"
  # Moving the alias is NOT done by default: it republishes the api function's code as well as
  # its environment, which is a bigger change than a knob edit. Set OC_PUBLISH_API_ALIAS=1 to
  # opt in. Either way `verify` reads the version the alias actually serves, so if you skip it
  # you SEE that the api side did not take the new value rather than assuming it did.
  api_alias=""
  [ "${OC_PUBLISH_API_ALIAS:-0}" = "1" ] && api_alias=live
  case "$MODE" in
  plan)
    note "api=$api_fn  consumer=$consumer_fn"
    note "$KEY1: code default 6 -> $VAL1   $KEY2: code default 0.5 -> $VAL2"
    note "effect: a batch spreads over at most $VAL1 hosts instead of 6, and there is one"
    note "SendCommand per host — so at most $VAL1 calls per batch instead of 6, with each call"
    note "carrying proportionally more tenants."
    note "neither knob is injected by the stack today, so the current effective value is the"
    note "code default in core/clients.py — this op is the first thing to set them."
    if [ -n "$api_alias" ]; then
      note "OC_PUBLISH_API_ALIAS=1 — will publish a version and move alias 'live' on $api_fn"
    else
      note "alias move is OFF (default). The api function's \$LATEST will carry the new value"
      note "while alias 'live' keeps serving the frozen env of its published version. verify"
      note "will report that as a failure on the api side; that report is the point."
    fi
    ;;
  apply)
    env_set_one "$api_fn" "$api_alias"
    env_set_one "$consumer_fn" ""
    ;;
  verify)
    # Deliberately verify the consumer FIRST: it is the side that takes effect immediately, so
    # a failure there is unambiguous. The api side is checked against the version its alias
    # serves, which is what exposes the frozen-env trap.
    env_verify_one "$consumer_fn" ""
    env_verify_one "$api_fn" live
    note "both sides agree — a batch now spreads over at most $VAL1 hosts, i.e. at most $VAL1"
    note "SendCommand calls per batch instead of 6."
    ;;
  rollback)
    for fn in "$api_fn" ${consumer_fn:-}; do
      b="$STATE_DIR/env-before-$fn.json"
      [ -s "$b" ] || die "no pre-change env for $fn — refusing to guess its variable map"
      python3 -c 'import json,sys;json.dump({"Variables":json.load(open(sys.argv[1]))},open(sys.argv[2],"w"))' \
        "$b" "$STATE_DIR/env-restore-$fn.json"
      aws lambda update-function-configuration --function-name "$fn" \
        --environment "file://$STATE_DIR/env-restore-$fn.json" --query LastUpdateStatus --output text
      aws lambda wait function-updated --function-name "$fn"
      if [ -s "$STATE_DIR/alias-before-$fn.txt" ]; then
        aws lambda update-alias --function-name "$fn" --name live \
          --function-version "$(cat "$STATE_DIR/alias-before-$fn.txt")" >/dev/null
        note "$fn alias live restored to $(cat "$STATE_DIR/alias-before-$fn.txt")"
      fi
      note "$fn environment restored from the pre-change readback"
    done
    ;;
  esac
  ;;

*)
  echo "unknown op: $OP" >&2
  echo "ops: apigw-identify-live ssm-fence-lease-param iam-api-fence-param-read iam-health-describe-instances lambda-api-code lambda-health-code lambda-env-spread-and-floor codebuild-goldenimage-asset-drift" >&2
  exit 2
  ;;
esac
