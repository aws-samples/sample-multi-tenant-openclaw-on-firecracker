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

*)
  echo "unknown op: $OP" >&2
  echo "ops: ssm-fence-lease-param iam-api-fence-param-read iam-health-describe-instances lambda-api-code lambda-health-code codebuild-goldenimage-asset-drift" >&2
  exit 2
  ;;
esac
