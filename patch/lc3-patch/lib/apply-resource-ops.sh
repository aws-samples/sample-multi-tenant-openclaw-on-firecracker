#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apply-resource-ops.sh — the one executable path for every resource this kit owns. No stack
# update is involved: each operation is the by-hand AWS CLI equivalent, wrapped so that every
# executor (human or agent) runs identical code.
#
#   lib/apply-resource-ops.sh <op> <plan|apply|verify|rollback>
#
# Design rules this file follows, each one because its absence produced a real failure mode in
# review:
#
#  * Every op runs preflight first: the active CLI identity must equal the account in
#    environment.json and the region must be stated. Without that, a wrong profile silently
#    modifies a same-named resource in another account and verify reads that same wrong target
#    and passes.
#  * Every aws call passes --region explicitly. An ambient region is how a command lands in the
#    wrong place while looking correct.
#  * A read failure is NOT absence. Only an explicit not-found error means "not there"; any
#    other error (denied, throttled, network) is fatal, because treating denial as absence turns
#    an adopt into a create.
#  * State is write-once. A completed run refuses to be overwritten, so a second apply can never
#    destroy the rollback point of the first. Set OC_ALLOW_RERUN=1 only when you have decided
#    the existing state is worthless.
#  * Concurrency-sensitive writes carry the RevisionId they read, so a racing edit fails the
#    write instead of being silently discarded.
#  * rollback preflights every artifact it will need BEFORE its first write, so it cannot mutate
#    one function and then abort on missing state for the next.
#  * verify judges the target that actually serves traffic, and compares against the artifact
#    this kit ships — not against a mutable baseline.
set -euo pipefail

OP="${1:?op name required}"
MODE="${2:?mode required: plan|apply|verify|rollback}"
case "$MODE" in
plan | apply | verify | rollback) ;;
*)
  echo "unknown mode: $MODE (want plan|apply|verify|rollback)" >&2
  exit 2
  ;;
esac

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_JSON="${OC_ENV_JSON:-$KIT_DIR/environment.json}"
STATE_DIR="$KIT_DIR/.lc3-state/$OP"
DONE_MARKER="$STATE_DIR/.apply-complete"
PARAM_NAME="/openclaw/lifecycle/fence-lease-sec"
PARAM_DEFAULT=240
PARAM_FLOOR=210

die() {
  echo "STOP: $*" >&2
  exit 1
}
note() { echo "  $*"; }

# --------------------------------------------------------------- coordinates
env_field() {
  local key="$1" optional="${2:-}" value
  [ -f "$ENV_JSON" ] || die "environment.json not found at $ENV_JSON — run lib/discover-env.sh first"
  value="$(
    OC_KEY="$key" python3 - "$ENV_JSON" <<'PY'
import json, os, sys
key = os.environ["OC_KEY"]
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(3)
node = data
for part in key.split("."):
    if isinstance(node, dict) and part in node:
        node = node[part]
    else:
        node = None
        break
if node is None or isinstance(node, (dict, list)) or str(node).strip() == "":
    print("", end="")
else:
    print(str(node).strip(), end="")
PY
  )" || die "environment.json is not valid JSON"
  if [ -z "$value" ]; then
    [ -n "$optional" ] && return 0
    die "environment.json has no usable value for '$key' — resolve it before running $OP"
  fi
  printf '%s' "$value"
}

REGION=""
ACCOUNT=""

# Bind the run to one account and one region, proven against the live caller identity. Every op
# calls this before it reads or writes anything.
preflight() {
  REGION="$(env_field region)"
  ACCOUNT="$(env_field account)"
  case "$REGION" in
  *[!a-z0-9-]* | "") die "region '$REGION' is not a plausible region name" ;;
  esac
  case "$ACCOUNT" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) die "account '$ACCOUNT' is not a 12-digit account id" ;;
  esac
  local live
  live="$(aws sts get-caller-identity --region "$REGION" --query Account --output text)" ||
    die "cannot reach STS in $REGION — check credentials and network before continuing"
  [ "$live" = "$ACCOUNT" ] ||
    die "the active credentials are for account $live but environment.json says $ACCOUNT. Refusing: a same-named function or role exists in many accounts, and verify would read the same wrong target and pass."
  note "bound to account $ACCOUNT in $REGION (confirmed against the live caller identity)"
}

# aws_read <outfile> <args...> — 0 on success, 3 on an explicit not-found, fatal otherwise.
aws_read() {
  local out="$1"
  shift
  local err rc
  err="$(mktemp)"
  if aws "$@" >"$out" 2>"$err"; then
    rm -f "$err"
    return 0
  fi
  rc=1
  if grep -qiE "ResourceNotFoundException|NoSuchEntity|ParameterNotFound|does not exist|not found|NoSuchResource" "$err"; then
    rc=3
  else
    echo "--- aws error ---" >&2
    cat "$err" >&2
  fi
  rm -f "$err"
  [ "$rc" = 3 ] && return 3
  die "read failed and the error is not a not-found — refusing to treat it as absence (aws $1 $2)"
}

state_guard() {
  [ "$MODE" = apply ] || return 0
  if [ -f "$DONE_MARKER" ] && [ "${OC_ALLOW_RERUN:-0}" != "1" ]; then
    die "$OP already completed on $(cat "$DONE_MARKER") and its state is the rollback point for that run. Re-applying would overwrite it and leave you able to roll back only to the ALREADY-PATCHED state. Roll back first, or set OC_ALLOW_RERUN=1 if you have decided that state is worthless."
  fi
  mkdir -p "$STATE_DIR"
}
mark_done() { date -u +%Y-%m-%dT%H:%M:%SZ >"$DONE_MARKER"; }

# ------------------------------------------------------------------ IAM grant
iam_grant() {
  local mode="$1" role="$2" policy_name="$3" doc="$4" action="$5" resource="$6"
  local role_arn="arn:aws:iam::$ACCOUNT:role/$role" pre="$STATE_DIR/pre-$policy_name.$role.json"
  case "$mode" in
  plan)
    note "role=$role policy=$policy_name action=$action resource=$resource"
    ;;
  apply)
    # Snapshot any same-name inline policy FIRST. put-role-policy overwrites by name, so
    # without this a pre-existing policy of the same name is destroyed with no way back.
    if aws_read "$pre" iam get-role-policy --role-name "$role" --policy-name "$policy_name" --output json; then
      note "WARNING: $role already has an inline policy named $policy_name; its current document"
      note "is saved to $(basename "$pre") and rollback WILL restore it."
    else
      echo '{"absent":true}' >"$pre"
    fi
    local decision
    decision="$(aws iam simulate-principal-policy --policy-source-arn "$role_arn" \
      --action-names "$action" --resource-arns "$resource" \
      --query 'EvaluationResults[0].EvalDecision' --output text)" ||
      die "the policy simulator failed for $role — cannot tell whether the grant is needed"
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
      --query 'EvaluationResults[0].EvalDecision' --output text)" ||
      die "the policy simulator failed for $role"
    # The IAM data plane can lag the simulator by minutes; one allowed reading is the floor,
    # not proof. The per-fix check in APPLY Step 6 is what confirms real behaviour.
    [ "$decision" = "allowed" ] || die "$role is still $decision for $action on $resource"
    note "allowed: $role -> $action"
    ;;
  rollback)
    if [ -s "$pre" ] && ! grep -q '"absent"' "$pre"; then
      note "restoring the pre-existing inline policy $policy_name on $role that apply overwrote"
      python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));json.dump(d["PolicyDocument"],open(sys.argv[2],"w"))' \
        "$pre" "$STATE_DIR/restore-$policy_name.$role.json"
      aws iam put-role-policy --role-name "$role" --policy-name "$policy_name" \
        --policy-document "file://$STATE_DIR/restore-$policy_name.$role.json"
      return 0
    fi
    note "RETAIN — $policy_name on $role is a read-only grant that apply newly created, and"
    note "rolled-back code still reads the resource; removing it converts a working fallback"
    note "into AccessDenied. To remove deliberately:"
    note "  aws iam delete-role-policy --region $REGION --role-name $role --policy-name $policy_name"
    ;;
  esac
}

# ----------------------------------------------------------------- Lambda code
# Resolve the function name and prove it exists, so a typo or a wrong-account default cannot be
# written to. Prints the ARN so the operator sees exactly what will change.
resolve_fn() {
  local name="$1" arn
  arn="$(aws lambda get-function-configuration --region "$REGION" --function-name "$name" \
    --query FunctionArn --output text)" ||
    die "function '$name' not found in $ACCOUNT/$REGION — fix the coordinate, do not guess"
  case "$arn" in
  "arn:aws:lambda:$REGION:$ACCOUNT:function:$name") ;;
  *) die "resolved '$name' to $arn which is not in the bound account/region" ;;
  esac
  printf '%s' "$arn"
}

lambda_code() {
  local mode="$1" fn="$2" src="$3" alias_name="$4"
  local zip="$KIT_DIR/lc3-$fn.zip"
  case "$mode" in
  plan)
    note "function=$fn arn=$(resolve_fn "$fn") source=$src alias=${alias_name:-none}"
    note "zip=$zip $([ -f "$zip" ] && echo present || echo MISSING)"
    ;;
  apply)
    resolve_fn "$fn" >/dev/null
    [ -f "$zip" ] || die "$zip missing — build it in APPLY Step 2 before this op"
    local want_sha
    want_sha="$(openssl dgst -sha256 -binary "$zip" | openssl base64)"
    echo "$want_sha" >"$STATE_DIR/want-codesha-$fn.txt"
    aws lambda get-function-configuration --region "$REGION" --function-name "$fn" \
      --query '[RevisionId,CodeSha256,Version]' --output text >"$STATE_DIR/pre-$fn.txt"
    local url
    url="$(aws lambda get-function --region "$REGION" --function-name "$fn" \
      --query Code.Location --output text)"
    # --fail matters: without it an HTTP error body is written to the file and accepted as the
    # rollback artifact. unzip -t then proves the bytes are a real archive.
    curl -sS --fail "$url" -o "$STATE_DIR/backup-$fn.zip" ||
      die "could not download the current package for $fn — no verified backup, refusing to apply"
    unzip -t "$STATE_DIR/backup-$fn.zip" >"$STATE_DIR/backup-$fn.unziptest" 2>&1 ||
      die "the downloaded backup for $fn is not a valid archive — refusing to apply"
    aws lambda publish-version --region "$REGION" --function-name "$fn" \
      --description "pre-lc3-patch anchor" --query Version --output text \
      >"$STATE_DIR/anchor-$fn.txt" ||
      note "publish-version declined (Lambda does not republish an unchanged \$LATEST); the"
    note "downloaded bytes remain the authoritative rollback artifact"
    if [ -n "$alias_name" ]; then
      aws lambda get-alias --region "$REGION" --function-name "$fn" --name "$alias_name" \
        --query FunctionVersion --output text >"$STATE_DIR/alias-$fn.txt"
    fi
    note "backed up $fn (revision + verified bytes + version anchor)"
    aws lambda update-function-code --region "$REGION" --function-name "$fn" \
      --zip-file "fileb://$zip" --query '[LastUpdateStatus,CodeSha256]' --output text
    aws lambda wait function-updated --region "$REGION" --function-name "$fn"
    local got_sha
    got_sha="$(aws lambda get-function-configuration --region "$REGION" --function-name "$fn" \
      --query CodeSha256 --output text)"
    [ "$got_sha" = "$want_sha" ] ||
      die "$fn CodeSha256 is $got_sha but the zip this kit shipped hashes to $want_sha — the function is NOT running the packaged bytes"
    if [ -n "$alias_name" ]; then
      # Pin the publish to the sha we just verified, so a concurrent deploy of unrelated code
      # onto $LATEST cannot be promoted to the serving alias by this op.
      local newver
      newver="$(aws lambda publish-version --region "$REGION" --function-name "$fn" \
        --code-sha256 "$want_sha" --query Version --output text)" ||
        die "publish-version refused: \$LATEST no longer hashes to the bytes this op wrote, which means something else changed the function mid-flight. Not promoting anything."
      aws lambda update-alias --region "$REGION" --function-name "$fn" --name "$alias_name" \
        --function-version "$newver" >/dev/null
      note "alias $alias_name -> version $newver"
      # Only NOW is the served target the new code, so invoke-verify happens here.
      local err
      err="$(aws lambda invoke --region "$REGION" --function-name "$fn:$alias_name" \
        --payload '{"httpMethod":"GET","path":"/ping"}' --cli-binary-format raw-in-base64-out \
        /dev/null --query FunctionError --output text)"
      [ "$err" = "None" ] || die "$fn:$alias_name returned FunctionError=$err after the alias move"
      note "invoke of the SERVED target reports FunctionError=None"
    else
      note "no alias: \$LATEST is what serves (its event source mapping binds \$LATEST), so the"
      note "sha check above is against the serving target already."
    fi
    ;;
  verify)
    local want target
    want="$(cat "$STATE_DIR/want-codesha-$fn.txt" 2>/dev/null || true)"
    [ -n "$want" ] ||
      die "no expected CodeSha256 recorded for $fn — verify has no baseline and must not pass"
    target="$fn"
    if [ -n "$alias_name" ]; then
      local ver
      ver="$(aws lambda get-alias --region "$REGION" --function-name "$fn" --name "$alias_name" \
        --query FunctionVersion --output text)"
      target="$fn:$ver"
      note "$fn serves version $ver through alias $alias_name — judging THAT version"
    fi
    local now
    now="$(aws lambda get-function-configuration --region "$REGION" --function-name "$target" \
      --query CodeSha256 --output text)"
    [ "$now" = "$want" ] ||
      die "$target runs CodeSha256 $now but this kit shipped $want — the serving target is not the packaged bytes"
    note "$target runs exactly the bytes this kit ships ($now)"
    ;;
  rollback)
    [ -s "$STATE_DIR/backup-$fn.zip" ] ||
      die "no backup zip for $fn in $STATE_DIR — refusing to roll back to an invented target"
    if [ -n "$alias_name" ]; then
      [ -s "$STATE_DIR/alias-$fn.txt" ] ||
        die "$fn has alias $alias_name but no recorded pre-change alias version — refusing a partial rollback that would leave the alias on patched code"
      aws lambda update-alias --region "$REGION" --function-name "$fn" --name "$alias_name" \
        --function-version "$(cat "$STATE_DIR/alias-$fn.txt")" >/dev/null
      note "alias $alias_name restored to $(cat "$STATE_DIR/alias-$fn.txt")"
    fi
    # Both halves: an SQS event source mapping binds $LATEST, so restoring only the alias
    # leaves the consumer path on new code.
    aws lambda update-function-code --region "$REGION" --function-name "$fn" \
      --zip-file "fileb://$STATE_DIR/backup-$fn.zip" --query LastUpdateStatus --output text
    aws lambda wait function-updated --region "$REGION" --function-name "$fn"
    note "\$LATEST restored from the verified backup bytes"
    ;;
  esac
}

# ------------------------------------------------------------------------ ops
preflight
state_guard

case "$OP" in
ssm-fence-lease-param)
  BEFORE="$STATE_DIR/before.txt"
  case "$MODE" in
  plan) note "parameter=$PARAM_NAME default=$PARAM_DEFAULT floor=$PARAM_FLOOR" ;;
  apply)
    if aws_read "$BEFORE" ssm get-parameter --region "$REGION" --name "$PARAM_NAME" \
      --query Parameter.Value --output text; then
      note "parameter already exists with value $(cat "$BEFORE") — adopting it"
      note "(raising it is a deliberate, separate decision; this op does not overwrite)"
    else
      : >"$BEFORE"
      aws ssm put-parameter --region "$REGION" --name "$PARAM_NAME" --type String \
        --value "$PARAM_DEFAULT" \
        --description "openclaw lifecycle fence lease in seconds. Takes effect immediately with no stack update. Read by the api and lifecycle-consumer functions with a 60s in-process cache. Floor is 210s, the longest fenced action's execution budget." \
        --query Version --output text
      note "created $PARAM_NAME = $PARAM_DEFAULT"
    fi
    mark_done
    ;;
  verify)
    value="$(aws ssm get-parameter --region "$REGION" --name "$PARAM_NAME" \
      --query Parameter.Value --output text)" ||
      die "$PARAM_NAME is unreadable — apply did not land, or the caller cannot read it"
    case "$value" in
    '' | *[!0-9]*) die "$PARAM_NAME is not an integer: '$value'" ;;
    esac
    [ "$value" -ge "$PARAM_FLOOR" ] ||
      die "$PARAM_NAME=$value is below the floor $PARAM_FLOOR — the runtime rejects it and falls back to $PARAM_DEFAULT, so this value silently does not apply"
    note "$PARAM_NAME = $value (>= $PARAM_FLOOR)"
    ;;
  rollback)
    [ -f "$BEFORE" ] || die "no pre-state — refusing to guess the previous value"
    if [ -s "$BEFORE" ]; then
      aws ssm put-parameter --overwrite --region "$REGION" --name "$PARAM_NAME" --type String \
        --value "$(cat "$BEFORE")" --query Version --output text
      note "restored $PARAM_NAME to $(cat "$BEFORE")"
    else
      aws ssm delete-parameter --region "$REGION" --name "$PARAM_NAME"
      note "deleted $PARAM_NAME (this op created it; the code falls back to $PARAM_DEFAULT)"
    fi
    ;;
  esac
  ;;

iam-api-fence-param-read)
  PARAM_ARN="arn:aws:ssm:$REGION:$ACCOUNT:parameter$PARAM_NAME"
  API_ROLE="$(env_field api_role)"
  CONSUMER_ROLE="$(env_field consumer_role optional || true)"
  iam_grant "$MODE" "$API_ROLE" lc3-lifecycle-fence-lease-param-read \
    "$KIT_DIR/iam/lifecycle-fence-lease-param-read.json" ssm:GetParameter "$PARAM_ARN"
  if [ -n "${CONSUMER_ROLE:-}" ]; then
    iam_grant "$MODE" "$CONSUMER_ROLE" lc3-lifecycle-fence-lease-param-read \
      "$KIT_DIR/iam/lifecycle-fence-lease-param-read.json" ssm:GetParameter "$PARAM_ARN"
  elif [ "$MODE" = apply ] || [ "$MODE" = verify ]; then
    die "no consumer_role in environment.json. The consumer is the primary executor of async lifecycle actions: granting only the api role produces the worst shape, where api reads the parameter and consumer silently falls back to its code default and the two diverge the moment the value is edited. Resolve the coordinate."
  fi
  [ "$MODE" = apply ] && mark_done
  ;;

iam-health-describe-instances)
  HEALTH_ROLE="$(env_field health_role)"
  # ec2:Describe* has no resource-level IAM, so the resource is "*" and the statement is
  # read-only. Without it the reaper cannot confirm host death against EC2 and falls back to the
  # hosts-table clue, which can mark a tenant whose VM is still running as terminal.
  iam_grant "$MODE" "$HEALTH_ROLE" lc3-health-describe-instances \
    "$KIT_DIR/iam/health-describe-instances.json" ec2:DescribeInstances '*'
  [ "$MODE" = apply ] && mark_done
  ;;

lambda-api-code)
  API_FN="$(env_field api_function)"
  CONSUMER_FN="$(env_field consumer_function optional || true)"
  if [ -z "${CONSUMER_FN:-}" ] && { [ "$MODE" = apply ] || [ "$MODE" = verify ]; }; then
    die "no consumer_function in environment.json. It runs the SAME api source tree and is the primary executor of async lifecycle actions, so patching only the api function leaves the two on different code. Resolve the coordinate."
  fi
  if [ "$MODE" = rollback ]; then
    # Preflight every artifact before the first write, so we cannot restore api and then abort.
    for f in "$API_FN" ${CONSUMER_FN:+"$CONSUMER_FN"}; do
      [ -s "$STATE_DIR/backup-$f.zip" ] || die "rollback preflight: no backup zip for $f"
    done
    [ -s "$STATE_DIR/alias-$API_FN.txt" ] ||
      die "rollback preflight: no recorded pre-change live-alias version for $API_FN"
  fi
  lambda_code "$MODE" "$API_FN" lambda/api live
  [ -n "${CONSUMER_FN:-}" ] && lambda_code "$MODE" "$CONSUMER_FN" lambda/api ""
  [ "$MODE" = apply ] && mark_done
  ;;

lambda-health-code)
  HEALTH_FN="$(env_field health_function)"
  lambda_code "$MODE" "$HEALTH_FN" lambda/health_check ""
  [ "$MODE" = apply ] && mark_done
  ;;

lambda-env-spread-and-floor)
  # Lower the ssm:SendCommand call rate: SPREAD_MAX_HOSTS_PER_BATCH 6 -> 3 and
  # HOST_SELECTION_SCORE_FLOOR 0.5 -> 0.25 on BOTH functions. core/clients.py already reads both
  # from the environment with those code defaults, so this is configuration only.
  KEY1=SPREAD_MAX_HOSTS_PER_BATCH
  VAL1=3
  KEY2=HOST_SELECTION_SCORE_FLOOR
  VAL2=0.25
  API_FN="$(env_field api_function optional || true)"
  API_FN="${API_FN:-openclaw-api}"
  CONSUMER_FN="$(env_field consumer_function optional || true)"
  CONSUMER_FN="${CONSUMER_FN:-openclaw-lifecycle-consumer}"
  API_ALIAS=""
  [ "${OC_PUBLISH_API_ALIAS:-0}" = "1" ] && API_ALIAS=live

  env_set_one() {
    local fn="$1" alias_name="$2"
    resolve_fn "$fn" >/dev/null
    local before="$STATE_DIR/env-before-$fn.json" rev="$STATE_DIR/env-revid-$fn.txt"
    local cfg="$STATE_DIR/cfg-$fn.json"
    aws lambda get-function-configuration --region "$REGION" --function-name "$fn" \
      --output json >"$cfg" || die "cannot read the configuration of $fn"
    python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));json.dump((d.get("Environment") or {}).get("Variables") or {},open(sys.argv[2],"w"))' \
      "$cfg" "$before"
    python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["RevisionId"])' "$cfg" >"$rev"
    local n
    n="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$before")"
    [ "$n" -gt 0 ] ||
      die "read back 0 variables for $fn — writing now would wipe its environment; investigate before retrying"
    note "$fn has $n variable(s) at revision $(cat "$rev"); merging 2 keys into that map"
    local merged="$STATE_DIR/env-merged-$fn.json"
    OC_K1="$KEY1" OC_V1="$VAL1" OC_K2="$KEY2" OC_V2="$VAL2" \
      python3 - "$before" "$merged" <<'PY'
import json, os, sys
cur = json.load(open(sys.argv[1]))
cur[os.environ["OC_K1"]] = os.environ["OC_V1"]
cur[os.environ["OC_K2"]] = os.environ["OC_V2"]
json.dump({"Variables": cur}, open(sys.argv[2], "w"), indent=1, sort_keys=True)
print("  merged map has %d variable(s)" % len(cur))
PY
    # --revision-id makes the write fail instead of silently discarding a concurrent edit made
    # between the read above and this call. update-function-configuration replaces the whole
    # Variables map, so a lost racing edit is a deleted variable.
    aws lambda update-function-configuration --region "$REGION" --function-name "$fn" \
      --environment "file://$merged" --revision-id "$(cat "$rev")" \
      --query "Environment.Variables.{S:$KEY1,F:$KEY2}" --output json ||
      die "the write for $fn was rejected. If this is a PreconditionFailed, something changed the function between the read and the write — re-run so the merge is based on current state rather than overwriting that change."
    aws lambda wait function-updated --region "$REGION" --function-name "$fn"
    if [ -n "$alias_name" ]; then
      aws lambda get-alias --region "$REGION" --function-name "$fn" --name "$alias_name" \
        --query FunctionVersion --output text >"$STATE_DIR/alias-before-$fn.txt"
      local live_sha latest_sha
      live_sha="$(aws lambda get-function-configuration --region "$REGION" \
        --function-name "$fn:$(cat "$STATE_DIR/alias-before-$fn.txt")" \
        --query CodeSha256 --output text)"
      latest_sha="$(aws lambda get-function-configuration --region "$REGION" \
        --function-name "$fn" --query CodeSha256 --output text)"
      [ "$live_sha" = "$latest_sha" ] ||
        die "\$LATEST of $fn holds code ($latest_sha) that differs from what alias $alias_name serves ($live_sha). Publishing now would promote that unrelated code along with this configuration change. Reconcile the code first, or apply this op without OC_PUBLISH_API_ALIAS and accept consumer-side-only."
      local newver
      newver="$(aws lambda publish-version --region "$REGION" --function-name "$fn" \
        --code-sha256 "$latest_sha" --query Version --output text)"
      aws lambda update-alias --region "$REGION" --function-name "$fn" --name "$alias_name" \
        --function-version "$newver" >/dev/null
      note "$fn: published version $newver (same code as before, new env) and moved $alias_name"
    else
      note "$fn: no alias — the event source mapping binds \$LATEST, so this is already live"
    fi
  }

  env_verify_one() {
    # Two statements on purpose. Names declared in ONE `local` are not visible to each other,
    # so `local fn="$1" target="$fn"` reads an unset fn and `set -u` kills the run with
    # "fn: unbound variable". This is not a bash 3.2 quirk — 5.x behaves the same.
    local fn="$1" alias_name="$2"
    local target="$fn"
    if [ -n "$alias_name" ]; then
      local ver
      ver="$(aws lambda get-alias --region "$REGION" --function-name "$fn" --name "$alias_name" \
        --query FunctionVersion --output text)"
      target="$fn:$ver"
      note "$fn serves version $ver through alias $alias_name — reading THAT version's env"
    fi
    local got1 got2
    got1="$(aws lambda get-function-configuration --region "$REGION" --function-name "$target" \
      --query "Environment.Variables.$KEY1" --output text)"
    got2="$(aws lambda get-function-configuration --region "$REGION" --function-name "$target" \
      --query "Environment.Variables.$KEY2" --output text)"
    [ "$got1" = "$VAL1" ] ||
      die "$target has $KEY1=$got1, want $VAL1 (None means the served version predates the change)"
    [ "$got2" = "$VAL2" ] || die "$target has $KEY2=$got2, want $VAL2"
    # A merge that lost variables is a silent outage; count them against the pre-change map.
    local before="$STATE_DIR/env-before-$fn.json"
    if [ -s "$before" ]; then
      local was now_n
      was="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$before")"
      now_n="$(aws lambda get-function-configuration --region "$REGION" --function-name "$target" \
        --query 'length(keys(Environment.Variables))' --output text)"
      [ "$now_n" -ge "$((was + 0))" ] ||
        die "$target now has $now_n variables but had $was before — the map lost keys, which means a whole-map replacement happened instead of a merge"
      note "$target keeps all $was pre-change variable(s) plus the two knobs"
    fi
    note "$target: $KEY1=$got1 $KEY2=$got2"
  }

  case "$MODE" in
  plan)
    note "api=$API_FN  consumer=$CONSUMER_FN"
    note "$KEY1: code default 6 -> $VAL1   $KEY2: code default 0.5 -> $VAL2"
    note "effect: a batch spreads over at most $VAL1 hosts instead of 6, and there is one"
    note "SendCommand per host — at most $VAL1 calls per batch instead of 6, each carrying"
    note "proportionally more tenants."
    if [ -n "$API_ALIAS" ]; then
      note "OC_PUBLISH_API_ALIAS=1 — will publish and move alias 'live' on $API_FN, but ONLY"
      note "after proving \$LATEST holds the same code the alias already serves."
    else
      note "alias move is OFF (default). \$LATEST of $API_FN will carry the new value while"
      note "alias 'live' keeps serving its frozen env. apply will say so loudly and verify will"
      note "report the api side as failing — that report is the intended outcome, not a bug."
    fi
    ;;
  apply)
    env_set_one "$API_FN" "$API_ALIAS"
    env_set_one "$CONSUMER_FN" ""
    if [ -z "$API_ALIAS" ]; then
      echo
      echo "  ATTENTION — this apply deliberately left the two sides on different values:"
      echo "    $CONSUMER_FN  : $KEY1=$VAL1 $KEY2=$VAL2  (live now)"
      echo "    $API_FN       : \$LATEST updated, but alias 'live' still serves its frozen env"
      echo "  The consumer is where the async create path runs, so the SendCommand reduction is"
      echo "  in effect there. Decide explicitly: accept consumer-side-only, or re-run with"
      echo "  OC_ALLOW_RERUN=1 OC_PUBLISH_API_ALIAS=1 after Step 2 has landed and been verified."
    fi
    mark_done
    ;;
  verify)
    # Consumer first: it takes effect immediately, so a failure there is unambiguous.
    env_verify_one "$CONSUMER_FN" ""
    env_verify_one "$API_FN" live
    note "both sides agree — a batch now spreads over at most $VAL1 hosts, i.e. at most $VAL1"
    note "SendCommand calls per batch instead of 6."
    ;;
  rollback)
    # Preflight both before writing either.
    for fn in "$API_FN" "$CONSUMER_FN"; do
      [ -s "$STATE_DIR/env-before-$fn.json" ] ||
        die "rollback preflight: no pre-change env for $fn — refusing to guess its variable map"
    done
    for fn in "$API_FN" "$CONSUMER_FN"; do
      b="$STATE_DIR/env-before-$fn.json"
      python3 -c 'import json,sys;json.dump({"Variables":json.load(open(sys.argv[1]))},open(sys.argv[2],"w"))' \
        "$b" "$STATE_DIR/env-restore-$fn.json"
      cur_rev="$(aws lambda get-function-configuration --region "$REGION" --function-name "$fn" \
        --query RevisionId --output text)"
      aws lambda update-function-configuration --region "$REGION" --function-name "$fn" \
        --environment "file://$STATE_DIR/env-restore-$fn.json" --revision-id "$cur_rev" \
        --query LastUpdateStatus --output text ||
        die "restore of $fn was rejected — read its current state and reconcile by hand rather than forcing a stale map over a newer one"
      aws lambda wait function-updated --region "$REGION" --function-name "$fn"
      if [ -s "$STATE_DIR/alias-before-$fn.txt" ]; then
        aws lambda update-alias --region "$REGION" --function-name "$fn" --name live \
          --function-version "$(cat "$STATE_DIR/alias-before-$fn.txt")" >/dev/null
        note "$fn alias live restored to $(cat "$STATE_DIR/alias-before-$fn.txt")"
      fi
      note "$fn environment restored from the pre-change readback"
      note "NOTE: this writes the map as it was BEFORE apply. Any variable legitimately added"
      note "since then is removed by this restore — check the diff if time has passed."
    done
    ;;
  esac
  ;;

codebuild-goldenimage-asset-drift)
  # The golden-image builder's source asset key moves because the builder packages the
  # repository tree and this range changed files inside it. The only difference is that object
  # key. Nothing on the running system depends on it and this kit uploads no such asset, so the
  # correct action now is to change nothing and record the current value so a later bake can be
  # attributed. That is why the op requires operator review.
  PROJECT="$(env_field golden_image_project optional || true)"
  SRCFILE="$STATE_DIR/source-location.txt"
  case "$MODE" in
  plan | apply)
    [ -n "${PROJECT:-}" ] || die "no golden_image_project in environment.json — resolve it, or drop this op from the run and say so; silently skipping a reviewed operation is how a record goes missing"
    mkdir -p "$STATE_DIR"
    aws codebuild batch-get-projects --region "$REGION" --names "$PROJECT" \
      --query 'projects[0].source.location' --output text >"$SRCFILE"
    note "recorded builder source: $(cat "$SRCFILE")"
    note "This kit changes nothing here. The new asset key comes into existence at the next"
    note "image bake, which must take its source from the patched tree."
    [ "$MODE" = apply ] && mark_done
    ;;
  verify)
    [ -n "${PROJECT:-}" ] || die "no golden_image_project — verify has no target and must not pass"
    [ -s "$SRCFILE" ] || die "no recorded baseline for $PROJECT — run plan first; a verify without a baseline cannot fail and is worthless"
    now="$(aws codebuild batch-get-projects --region "$REGION" --names "$PROJECT" \
      --query 'projects[0].source.location' --output text)"
    [ "$now" = "$(cat "$SRCFILE")" ] ||
      die "the builder source moved from $(cat "$SRCFILE") to $now — this kit does not change it, so something else did"
    note "builder source unchanged by this kit: $now"
    ;;
  rollback)
    note "RETAIN — nothing was changed, so there is nothing to roll back."
    ;;
  esac
  ;;

apigw-identify-live)
  # READ-ONLY. Pick the control-plane API by OBSERVED TRAFFIC, never by name, then read its
  # resource policy and print what a call that policy would admit.
  #
  # Why traffic and not a name: an account commonly holds several REST APIs with plausible names
  # (an old one, a redeployed one, a copy from a rehearsal). A name match that picks the wrong
  # one makes every later probe test the wrong system and read as "the fix is not live". A 403 is
  # equally ambiguous alone -- wrong API, wrong auth, or a policy that excludes your source --
  # which is why this op prints the policy instead of guessing.
  case "$MODE" in
  plan | verify)
    mkdir -p "$STATE_DIR"
    end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    start="$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
    echo "REST APIs in $ACCOUNT/$REGION, with request count over the last 7 days:"
    aws apigateway get-rest-apis --region "$REGION" --query 'items[].[id,name]' --output text |
      while read -r api_id api_name; do
        [ -n "$api_id" ] || continue
        count="$(aws cloudwatch get-metric-statistics --region "$REGION" \
          --namespace AWS/ApiGateway --metric-name Count \
          --dimensions "Name=ApiName,Value=$api_name" \
          --start-time "$start" --end-time "$end" --period 604800 --statistics Sum \
          --query 'Datapoints[0].Sum' --output text 2>/dev/null)" || count=unknown
        case "$count" in None | "") count=0 ;; esac
        printf '  requests=%-12s id=%s  name=%s\n' "$count" "$api_id" "$api_name"
      done | tee "$STATE_DIR/api-traffic.txt"
    echo
    echo "A zero-request API is not necessarily wrong (a private control plane can be idle), but"
    echo "an API with traffic you were NOT going to target is a red flag — reconcile first."
    echo
    configured="$(env_field api_id optional || true)"
    if [ -z "${configured:-}" ]; then
      echo "environment.json carries no api_id. Take the URL your deployed client configuration"
      echo "actually uses (PRIVATE_API_URL / CTRL_API_BASE), extract its api id, and record it as"
      echo "api_id — do not adopt the top row above on its own."
      [ "$MODE" = verify ] && die "verify needs api_id to check anything; without it this op cannot fail and must not pass"
      exit 0
    fi
    echo "Resource policy of the configured API ($configured):"
    aws apigateway get-rest-api --region "$REGION" --rest-api-id "$configured" \
      --query 'policy' --output text >"$STATE_DIR/resource-policy.raw" ||
      die "cannot read API $configured in $ACCOUNT/$REGION — wrong api_id, or the caller lacks apigateway:GET"
    python3 - "$STATE_DIR/resource-policy.raw" <<'PY'
import json, sys
raw = open(sys.argv[1]).read().strip()
if not raw or raw == "None":
    print("  NO resource policy. For a private API the VPC endpoint policy and IAM are then the")
    print("  only controls; for a regional or edge API it means anyone who can reach it and holds")
    print("  a valid key is admitted.")
    raise SystemExit(0)
try:
    pol = json.loads(raw.replace("\\", ""))
except Exception as exc:
    print("  could not parse the policy (%s). Raw text is in resource-policy.raw." % exc)
    raise SystemExit(0)
for st in pol.get("Statement", []):
    print("  %s %s" % (st.get("Effect"), st.get("Action")))
    for op, kv in (st.get("Condition") or {}).items():
        for key, val in kv.items():
            print("      %s %s = %s" % (op, key, val))
print()
print("  Build the probe call from those conditions, in this order:")
print("   1. aws:SourceVpce  -> the call must come from inside that VPC endpoint. curl from a")
print("      bastion in the VPC; from a laptop it is 403 regardless of credentials, and that")
print("      403 says nothing about the fix.")
print("   2. aws:SourceIp    -> the call must originate in that range.")
print("   3. aws:PrincipalArn / execute-api:* -> sign SigV4 as that principal.")
print("   4. If the API also requires a key, send x-api-key; a missing key is a 403 that looks")
print("      identical to a policy denial.")
PY
    echo
    echo "SigV4 (AWS_IAM) methods — how signing works on THIS tree:"
    bff="$KIT_DIR/../../deploy/console-bff/sigv4-client.mjs"
    if [ -f "$bff" ]; then
      echo "  The console BFF imports SignatureV4 from:"
      grep -m1 'from "@' "$bff" | sed 's/^/    /'
      echo "  and its own header records that the module is NOT bundled: on nodejs20.x the runtime"
      echo "  provides it (verified on a real function). So the check is 'does the import succeed"
      echo "  on the target runtime', not 'is a package installed here':"
      echo "    aws lambda invoke --region $REGION --function-name <the bff function> \\"
      echo "      --payload '{\"probe\":\"sigv4\"}' /dev/null --query FunctionError   # want None"
      echo "  Signing by hand from a shell: use awscurl, or 'aws apigateway test-invoke-method'"
      echo "  for a read-only method. Do not hand-roll a signature."
    else
      echo "  deploy/console-bff/sigv4-client.mjs is not in this checkout; skip this block."
    fi
    echo
    echo "  Three gates that circulate for this do NOT hold here — do not run them:"
    echo "   - deploy/console-bff has no package.json, so 'npm ci --omit=dev' has nothing to do."
    echo "   - the import is @aws-sdk/signature-v4, NOT @smithy/signature-v4, so a"
    echo "     'test -d node_modules/@smithy/signature-v4' gate fails on a healthy tree."
    echo "   - deploy/cdk-cli does not exist on this branch, so a pinned-cdk-version gate cannot"
    echo "     run. This kit never updates a stack, so no CDK version is on its critical path;"
    echo "     the synth provenance is bound in resources/cloudformation/*.assembly-index.json."
    ;;
  apply | rollback)
    note "read-only op — nothing to apply or roll back. Use: plan (or verify)."
    ;;
  esac
  ;;

*)
  echo "unknown op: $OP" >&2
  echo "ops: apigw-identify-live ssm-fence-lease-param iam-api-fence-param-read iam-health-describe-instances lambda-api-code lambda-health-code lambda-env-spread-and-floor codebuild-goldenimage-asset-drift" >&2
  exit 2
  ;;
esac
