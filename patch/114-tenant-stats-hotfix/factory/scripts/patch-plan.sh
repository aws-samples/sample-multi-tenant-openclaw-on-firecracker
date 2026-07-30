#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# `plan` — the terraform-plan step for a patch. READ-ONLY, and it answers everything an operator
# needs to decide BEFORE the first write:
#
#   what will change, on which resources, in which order
#   which permissions the run needs, and whether this caller actually has them
#   how the live system currently differs from what the patch assumes (conflicts)
#   how the canary is chosen and what signal will judge it
#   which parameters apply needs, and which are still unanswered
#
# Contract mirrors terraform: `plan` never mutates, its output is the thing you approve, and
# `apply` refuses to run a plan whose inputs changed. The plan is written to PLAN.json so
# `autopatch.sh` can bind to it.
#
# Usage: patch-plan.sh <kit-dir> <environment.json>
set -euo pipefail

KIT="${1:?usage: patch-plan.sh <kit-dir> <environment.json>}"
ENVJSON="${2:?usage: patch-plan.sh <kit-dir> <environment.json>}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Same hard requirement preflight enforces, checked here too because plan is the FIRST thing an
# operator runs and can be run from a workstation. macOS ships bash 3.2, which has no `mapfile`;
# without this the host branch died with "mapfile: command not found" at exit 127, which reads
# like a broken script rather than "you are on the wrong shell".
if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
  echo "FATAL: bash ${BASH_VERSION} is too old; this needs bash 4+ (mapfile)." >&2
  echo "  macOS: run this from the bastion, or use a brew-installed bash 5." >&2
  exit 3
fi

REGION="$(jq -r '.region // ""' "$ENVJSON")"
ACCOUNT="$(jq -r '(.account // "") | tostring' "$ENVJSON")"
KIT_ID="$(jq -r '.id' "$KIT/manifest.json")"
PATCH_SHA="$(jq -r '.patch_sha' "$KIT/manifest.json")"
# Each lane gets its own permission set, conflict checks and rollout description. Recognising
# only `fn-*` once made a DynamoDB kit fall through to the host branch, where it looked for a host
# snapshot and an ASG it does not have and reported unknowns for checks that do not apply to it.
# Lane resolution is centralized in _lanes.sh so this cannot drift from the other drivers.
# shellcheck source=_lanes.sh
source "$HERE/_lanes.sh"
KIND="$(oc_kit_lane "$KIT")"

CONFLICTS=0
UNKNOWNS=0
note() { printf '  %s\n' "$*"; }
conflict() { printf '  CONFLICT  %s\n' "$*" >&2; CONFLICTS=$((CONFLICTS+1)); }
unknown()  { printf '  UNKNOWN   %s\n' "$*" >&2; UNKNOWNS=$((UNKNOWNS+1)); }

printf '\n===== PLAN  %s  (%s)  patch_sha=%s =====\n' "$KIT_ID" "$KIND" "${PATCH_SHA:0:12}"

printf '\n-- 1. what changes --\n'
jq -r '.paths | to_entries[]
  | "  \(.value.change // "M")  \(.key)\n     -> \(
      (.value.activation.dest_path // "embedded in the compiled recipe")
    )  [\(.value.operations[0].class // "?")]"' "$KIT/manifest.json"

printf '\n-- 2. permissions this run needs --\n'
# Ask IAM rather than trying a mutating call. simulate-principal-policy has no side effect, and
# discovering a denial here costs a rerun instead of a half-applied fleet.
principal_arn() {
  local arn role
  arn="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || true)"
  case "$arn" in
    *:assumed-role/*)
      role="${arn#*:assumed-role/}"; role="${role%%/*}"
      printf 'arn:aws:iam::%s:role/%s' "$ACCOUNT" "$role" ;;
    *) printf '%s' "$arn" ;;
  esac
}
case "$KIND" in
  lambda) ACTIONS=(lambda:GetFunction lambda:UpdateFunctionCode lambda:PublishVersion
                   lambda:UpdateAlias lambda:InvokeFunction lambda:ListEventSourceMappings) ;;
  ddbnew) # Create-only: no rollback call to probe, because there is no rollback.
          ACTIONS=(dynamodb:CreateTable dynamodb:DescribeTable
                   dynamodb:UpdateContinuousBackups) ;;
  apigw)  # Exactly what apply calls: create the resource chain, put method+integration, deploy,
          # and repoint the stage on rollback. lambda:AddPermission because the route 502s
          # without an invoke permission for this API.
          ACTIONS=(apigateway:GET apigateway:POST apigateway:PUT apigateway:PATCH
                   lambda:AddPermission) ;;
  ddb)    # Only the actions the setting this kit owns actually calls; asking about the other
          # settings' actions would report a denial that never blocks this run.
          SETTING="$(jq -r '.ddb_settings[0].setting' "$KIT/manifest.json")"
          case "$SETTING" in
            ttl)   ACTIONS=(dynamodb:DescribeTimeToLive dynamodb:UpdateTimeToLive) ;;
            pitr)  ACTIONS=(dynamodb:DescribeContinuousBackups
                            dynamodb:UpdateContinuousBackups) ;;
            *)     ACTIONS=(dynamodb:DescribeTable dynamodb:UpdateTable) ;;
          esac ;;
  *)      ACTIONS=(ssm:SendCommand ssm:ListCommandInvocations ssm:DescribeInstanceInformation
                   s3:PutObject s3:GetObject s3:DeleteObject) ;;
esac
PRINCIPAL="$(principal_arn)"
if [[ -z "$PRINCIPAL" ]]; then
  unknown "cannot read the caller identity; permissions unverified"
else
  SIM="$(aws iam simulate-principal-policy --policy-source-arn "$PRINCIPAL" \
    --action-names "${ACTIONS[@]}" \
    --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text 2>/dev/null || true)"
  if [[ -z "$SIM" ]]; then
    unknown "iam:SimulatePrincipalPolicy unavailable; the run may fail on a denied call"
  else
    while IFS=$'\t' read -r act dec; do
      [[ -n "$act" ]] || continue
      if [[ "$dec" == "allowed" ]]; then note "allowed   $act"
      else conflict "$act is $dec — apply would fail partway"; fi
    done <<< "$SIM"
  fi
fi

printf '\n-- 3. conflicts with the live system --\n'
if [[ "$KIND" == "ddbnew" ]]; then
  TBL="$(jq -r '.ddb_tables[0].table' "$KIT/manifest.json")"
  LIVE_ST="$(aws dynamodb describe-table --region "$REGION" --table-name "$TBL" \
    --query 'Table.TableStatus' --output text 2>/dev/null || true)"
  if [[ -z "$LIVE_ST" || "$LIVE_ST" == "None" ]]; then
    note "$TBL does not exist; apply creates it and waits for ACTIVE"
  else
    note "$TBL already exists ($LIVE_ST); apply compares the key schema and SKIPs if it matches"
  fi
  note "cfn follow-up: $(jq -r '.ddb_tables[0].cfn_follow_up' "$KIT/manifest.json")"
elif [[ "$KIND" == "apigw" ]]; then
  AID="$(jq -r '.api_routes[0].api_id' "$KIT/manifest.json")"
  STG="$(jq -r '.api_routes[0].stage' "$KIT/manifest.json")"
  RPATH="$(jq -r '.api_routes[0].path' "$KIT/manifest.json")"
  RMETHOD="$(jq -r '.api_routes[0].method' "$KIT/manifest.json")"
  RQUAL="$(jq -r '.api_routes[0].target_qualifier // ""' "$KIT/manifest.json")"
  BASE_DEP="$(aws apigateway get-stage --region "$REGION" --rest-api-id "$AID" \
    --stage-name "$STG" --query deploymentId --output text 2>/dev/null || true)"
  if [[ -z "$BASE_DEP" || "$BASE_DEP" == "None" ]]; then
    conflict "stage $STG on $AID has no deployment — there would be nothing to roll back to"
  else
    note "stage $STG is on deployment $BASE_DEP (this is the rollback target)"
  fi
  # An existing route is the one real conflict here: applying would repoint live traffic that
  # someone else configured, and rollback could not tell whose integration it restored.
  EXIST="$(aws apigateway get-resources --region "$REGION" --rest-api-id "$AID" \
    --query "items[?path=='$RPATH'].id | [0]" --output text 2>/dev/null || true)"
  if [[ -n "$EXIST" && "$EXIST" != "None" ]]; then
    CUR="$(aws apigateway get-integration --region "$REGION" --rest-api-id "$AID" \
      --resource-id "$EXIST" --http-method "$RMETHOD" --query uri --output text 2>/dev/null || true)"
    if [[ -n "$CUR" && "$CUR" != "None" ]]; then
      conflict "$RMETHOD $RPATH already exists and integrates $CUR — this patch did not create it"
    else
      note "resource $RPATH exists but has no $RMETHOD method; apply will add it"
    fi
  else
    note "$RMETHOD $RPATH does not exist yet; apply creates the resource chain"
  fi
  if [[ -z "$RQUAL" ]]; then
    note "the new route invokes \$LATEST (no target_qualifier): it will serve whatever code is"
    note "on \$LATEST at the time, including an unverified Lambda patch mid-rollout"
  else
    note "the new route invokes qualifier '$RQUAL'"
  fi
elif [[ "$KIND" == "ddb" ]]; then
  TBL="$(jq -r '.ddb_settings[0].table' "$KIT/manifest.json")"
  SET_NAME="$(jq -r '.ddb_settings[0].setting' "$KIT/manifest.json")"
  WANT="$(jq -r '.ddb_settings[0].desired' "$KIT/manifest.json")"
  DECL_BASE="$(jq -r '.ddb_settings[0].baseline // ""' "$KIT/manifest.json")"
  STATUS="$(aws dynamodb describe-table --region "$REGION" --table-name "$TBL" \
    --query 'Table.TableStatus' --output text 2>/dev/null || true)"
  if [[ -z "$STATUS" ]]; then
    conflict "cannot read table $TBL — wrong account/region, or it does not exist"
  else
    note "table $TBL is $STATUS"
    [[ "$STATUS" == "ACTIVE" ]] ||
      conflict "the table is $STATUS; a configuration change needs it ACTIVE"
  fi
  case "$SET_NAME" in
    ttl)  LIVE="$(aws dynamodb describe-time-to-live --region "$REGION" --table-name "$TBL" \
            --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null || true)" ;;
    pitr) LIVE="$(aws dynamodb describe-continuous-backups --region "$REGION" --table-name "$TBL" \
            --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' \
            --output text 2>/dev/null || true)" ;;
    *)    LIVE="$(aws dynamodb describe-table --region "$REGION" --table-name "$TBL" \
            --query 'Table.DeletionProtectionEnabled' --output text 2>/dev/null || true)" ;;
  esac
  if [[ -z "$LIVE" ]]; then
    unknown "cannot read $SET_NAME on $TBL; the change cannot be planned against live state"
  elif [[ "$LIVE" == "$WANT" ]]; then
    note "$SET_NAME is already $LIVE — apply will be a no-op (idempotent)"
  elif [[ -n "$DECL_BASE" && "$LIVE" != "$DECL_BASE" ]]; then
    # Neither the patched value nor the baseline this kit was built against: rolling back would
    # restore a value that was never there.
    conflict "$SET_NAME on $TBL is '$LIVE'; the patch was built against '$DECL_BASE'"
  else
    note "$SET_NAME on $TBL is '$LIVE' -> '$WANT'  (rollback target: '$LIVE')"
  fi
  # A TTL change inside the AWS rate-limit window cannot be made yet, and finding that out at
  # apply time looks like a bug. The API does not expose the window, so this is a caution, not
  # a conflict.
  [[ "$SET_NAME" == "ttl" && "$LIVE" != "$WANT" ]] &&
    note "caution: DynamoDB allows one TTL change per table per ~1h; apply exits 26 if inside it"
elif [[ "$KIND" == "lambda" ]]; then
  FN="$(jq -r '.lambda_functions[0].function_name' "$KIT/manifest.json")"
  AL="$(jq -r '.lambda_functions[0].alias // "live"' "$KIT/manifest.json")"
  LIVE_SHA="$(aws lambda get-function-configuration --region "$REGION" \
    --function-name "$FN" --query CodeSha256 --output text 2>/dev/null || true)"
  [[ -n "$LIVE_SHA" ]] && note "live \$LATEST CodeSha256 = $LIVE_SHA" \
    || conflict "cannot read $FN — wrong account/region, or it does not exist"
  ALV="$(aws lambda get-alias --region "$REGION" --function-name "$FN" --name "$AL" \
    --query FunctionVersion --output text 2>/dev/null || echo NONE)"
  [[ "$ALV" == "NONE" ]] && conflict "alias $AL does not exist — apply has nothing to move" \
    || note "alias $AL currently on Version $ALV"
  # Which qualifier each async consumer runs. The CDK source renders an unqualified ARN, so a
  # kit that repoints this without a template follow-up is silently temporary.
  for arn in $(aws lambda list-event-source-mappings --region "$REGION" \
      --query "EventSourceMappings[?FunctionArn=='arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FN}' || starts_with(FunctionArn, 'arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FN}:')].FunctionArn" \
      --output text 2>/dev/null || true); do
    if [[ "$arn" == *":function:${FN}" ]]; then
      note "event source $arn -> \$LATEST (async NOT alias-gated)"
    else
      note "event source $arn -> follows the alias"
    fi
  done
  DECL="$(jq -r '.lambda_functions[0].esm_binding_conflict // "not-declared"' "$KIT/manifest.json")"
  note "esm_binding_conflict = $DECL"
  # Which API routes bypass the alias gate. Measured on this testbed: the private REST API
  # integrates ANY / and ANY /{proxy+} against the unqualified ARN, so all of its traffic is on
  # $LATEST and sees the new code before the verify. The operator has to read that BEFORE
  # approving, which is what the plan is for.
  set +e
  UNQ="$(python3 "$HERE/find-unqualified-routes.py" "$REGION" "$FN" 2>&1)"
  UNQ_RC=$?
  set -e
  if [[ "$UNQ_RC" -ne 0 ]]; then
    unknown "cannot enumerate API routes (${UNQ:0:120}) — alias coverage UNVERIFIED"
  elif [[ -z "$UNQ" ]]; then
    note "all API routes are alias-bound; no traffic sees \$LATEST before the verify"
  else
    while read -r r; do [[ -n "$r" ]] && note "\$LATEST route (not alias-gated): $r"; done <<< "$UNQ"
    note "the routes above see the new code as soon as \$LATEST is updated, i.e. BEFORE the"
    note "verify. Rollback reverts code and alias together, so recovery is still one command."
  fi
else
  mapfile -t HOSTS < <(jq -r '.hosts.instance_ids[]? // empty' "$ENVJSON")
  note "${#HOSTS[@]} host(s) in the DISCOVER snapshot"
  ONLINE="$(aws ssm describe-instance-information --region "$REGION" \
    --filters "Key=InstanceIds,Values=$(IFS=,; echo "${HOSTS[*]:-none}")" \
    --query 'length(InstanceInformationList[?PingStatus==`Online`])' --output text 2>/dev/null || echo 0)"
  [[ "$ONLINE" == "${#HOSTS[@]}" ]] && note "all ${#HOSTS[@]} Online in SSM" \
    || conflict "only $ONLINE of ${#HOSTS[@]} host(s) Online — the rest would silently miss the patch"
  ASG="$(jq -r '.asg.name // ""' "$ENVJSON")"
  OK="$(jq -r '.asg.confirmed // false' "$ENVJSON")"
  if [[ -n "$ASG" && "$OK" == "true" ]]; then
    TOTAL="$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
      --auto-scaling-group-names "$ASG" \
      --query 'length(AutoScalingGroups[0].Instances)' --output text 2>/dev/null || echo '?')"
    note "ASG $ASG currently owns $TOTAL instance(s); the snapshot covers ${#HOSTS[@]}"
    [[ "$TOTAL" == "${#HOSTS[@]}" ]] || conflict \
      "the ASG has $TOTAL instance(s) but the snapshot has ${#HOSTS[@]} — rerun DISCOVER, or hosts will be missed"
  else
    unknown "ASG unconfirmed; fleet coverage cannot be proven at the end of the run"
  fi
  # A lease held by someone else means another writer is mid-rollout.
  BUCKET="$(jq -r '.assets_bucket // .bindings.ASSETS_BUCKET // ""' "$ENVJSON")"
  if [[ -n "$BUCKET" ]] && aws s3api head-object --bucket "$BUCKET" \
      --key "patch-leases/fleet.lease" --region "$REGION" >/dev/null 2>&1; then
    conflict "a fleet patch lease already exists — another rollout may be in flight"
  else
    note "no fleet patch lease held"
  fi
fi

printf '\n-- 4. rollout plan --\n'
if [[ "$KIND" == "ddbnew" ]]; then
  note "one table, created if absent; no canary (a table is not a fleet)"
  note "apply waits for ACTIVE, because a write to a CREATING table fails"
  note "rerunning is a no-op: an existing table with the declared key schema SKIPs"
  note "THERE IS NO ROLLBACK. rollback.sh refuses and prints the deliberate-delete commands,"
  note "because undoing a table means deleting whatever has been written to it since."
elif [[ "$KIND" == "apigw" ]]; then
  note "one route; there is no canary — a stage deployment is all-or-nothing by definition"
  note "measured: changing the configuration does NOT change behavior; only create-deployment"
  note "does, and the new deployment took 15s to become visible, so verify polls to a deadline"
  note "rollback repoints the stage to the recorded deployment (a deployment is a config"
  note "snapshot, so this restores the old routing exactly, in one call)"
  note "rollback leaves the route CONFIGURATION in place; no traffic reaches it"
elif [[ "$KIND" == "ddb" ]]; then
  note "one table setting; there is no canary — the setting is table-wide by definition"
  note "apply records the live value as the rollback target BEFORE writing, then waits for the"
  note "API to report the new value (it transitions asynchronously) rather than trusting the call"
  note "rollback restores the recorded value, and refuses if the live value is someone else's"
elif [[ "$KIND" == "lambda" ]]; then
  note "one function; \$LATEST is updated and VERIFIED before the alias moves, so ALIAS-BOUND"
  note "callers stay on the current version if the build is bad (see section 3 for the routes"
  note "that bypass the alias and therefore do not get that protection)"
  note "rollback reverts BOTH code and alias"
else
  mapfile -t HOSTS < <(jq -r '.hosts.instance_ids[]? // empty' "$ENVJSON")
  note "canary: ${HOSTS[0]:-<none>}  (first host in the snapshot)"
  note "bake:   ${OC_PATCH_BAKE_SECONDS:-120}s, then a read-only verify re-observes the probe"
  note "fleet:  ${#HOSTS[@]} host(s) minus the canary, only after an explicit approval"
  jq -r '.paths | to_entries[] | select(.value.activation != null)
    | "  signal: \(.value.activation.effect_probe.kind) on \(.value.activation.effect_probe.path)"' \
    "$KIT/manifest.json"
fi

printf '\n-- 5. parameters apply will need --\n'
DEC="$KIT/DECISION.json"
if [[ -f "$DEC" ]]; then
  note "decision on file: $(jq -r '.question_count' "$DEC") question(s) answered"
  note "fleet_widening = $(jq -r '.fleet_widening' "$DEC")"
  python3 "$HERE/interview-once.py" check "$KIT" >/dev/null 2>&1 \
    || conflict "the recorded decision no longer matches this manifest — re-run the interview"
else
  unknown "no DECISION.json — run interview-once.py; apply will refuse without it"
fi
while read -r b; do
  [[ -n "$b" ]] || continue
  v="$(jq -r --arg n "$b" '.bindings[$n] // ""' "$ENVJSON")"
  [[ -n "$v" ]] && note "binding $b = $v" || conflict "binding $b is required but absent"
done < <(jq -r '.paths[].render.required_bindings[]? // empty' "$KIT/manifest.json" | sort -u)

# The plan is an artifact you approve, and apply binds to it. Recording the inputs' hashes is
# what makes "the plan is stale" detectable rather than a matter of trust.
PLAN="$KIT/PLAN.json"
jq -n --arg kit "$KIT_ID" --arg sha "$PATCH_SHA" --arg kind "$KIND" \
  --arg mhash "$(sha256sum "$KIT/manifest.json" | awk '{print $1}')" \
  --arg ehash "$(sha256sum "$ENVJSON" | awk '{print $1}')" \
  --argjson conflicts "$CONFLICTS" --argjson unknowns "$UNKNOWNS" \
  '{kit_id:$kit, patch_sha:$sha, kind:$kind, manifest_sha256:$mhash,
    environment_sha256:$ehash, conflicts:$conflicts, unknowns:$unknowns}' > "$PLAN"

printf '\n-- verdict --\n'
if [[ "$CONFLICTS" -gt 0 ]]; then
  printf '  PLAN_BLOCKED  %d conflict(s), %d unknown(s) — resolve before apply\n' \
    "$CONFLICTS" "$UNKNOWNS" >&2
  exit 30
fi
if [[ "$UNKNOWNS" -gt 0 ]]; then
  printf '  PLAN_INCOMPLETE  %d unknown(s) — apply may fail on something this plan could not check\n' \
    "$UNKNOWNS" >&2
  exit 31
fi
printf '  PLAN_OK  no conflicts; plan written to %s\n' "$PLAN"
