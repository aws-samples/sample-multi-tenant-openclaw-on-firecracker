#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# One-time preflight for an unattended patch run. READ-ONLY: it answers "is this environment
# the one you think it is, and can the runner physically work here" BEFORE anything is asked
# of the operator, so a run never dies halfway on a missing tool or a wrong account.
#
# Exit 0 = every check passed, the run may proceed to the interview.
# Any non-zero = stop. Nothing here writes, so a failure costs nothing but a rerun.
#
# Usage: preflight-once.sh <kit-dir> <environment.json>
set -euo pipefail

KIT="${1:?usage: preflight-once.sh <kit-dir> <environment.json>}"
ENVJSON="${2:?usage: preflight-once.sh <kit-dir> <environment.json>}"
FAIL=0
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lanes.sh
source "$HERE/_lanes.sh"

say() { printf '%s\n' "$*"; }
ok()  { printf '  PASS  %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*" >&2; FAIL=1; }

# simulate-principal-policy needs a PRINCIPAL arn. On an EC2/SSM host the caller is an
# assumed-role SESSION arn (arn:aws:sts::<acct>:assumed-role/<Role>/<session>), which the API
# rejects as "Invalid ARN" — measured on the real bastion. Normalize it to the underlying role
# so the probe works on a host, not only from a user's laptop.
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

# One verdict, one exit code, three lanes. Each lane used to end with its own copy of this
# block; a fourth lane would have added a fourth copy, and the copies were free to drift on
# which exit code means "failed".
verdict() {
  say
  if [[ "$FAIL" -eq 0 ]]; then
    say "PREFLIGHT_OK — environment verified, proceed to the interview."
    exit 0
  fi
  say "PREFLIGHT_FAILED — fix the items above and rerun. Nothing was changed." >&2
  exit 1
}

say "== 1. local tooling =="
# The generated recipes are pure Bash but they are not POSIX-portable: they use mapfile,
# printf -v and flock, so bash 4+ is a hard requirement (macOS ships bash 3.2).
if [[ "${BASH_VERSINFO[0]}" -ge 4 ]]; then ok "bash ${BASH_VERSION}"
else bad "bash ${BASH_VERSION} is too old — the recipes need bash 4+ (mapfile/printf -v)"; fi
for t in aws jq sha256sum base64 flock python3 git; do
  if command -v "$t" >/dev/null; then ok "$t"; else bad "missing $t"; fi
done

say "== 2. kit integrity =="
[[ -f "$KIT/manifest.json" ]] || bad "no manifest.json in $KIT"
# Two compiled kit shapes: a host-config kit has lib/compiled/recipe.json plus four
# top-level entrypoints; a control-plane (Lambda) kit has lib/compiled/<fn-id>/ with
# apply/verify/rollback and no recipe.json. Detect which, and check the right things —
# demanding recipe.json from a Lambda kit would reject a perfectly good compiled kit.
RECIPE="$KIT/lib/compiled/recipe.json"
# `find` exits non-zero when lib/compiled does not exist (a prose-era kit), and under
# `set -e` that killed the script mid-report — the operator saw a truncated run with no
# reason. Tolerate the miss and let the checks below say what is wrong.
# Which lanes exist is defined once in _lanes.sh; a control-plane lane takes its target from
# env vars rather than from the host snapshot.
LANE="$(oc_kit_lane "$KIT")"
LAMBDA_DIR=""
[[ "$LANE" != "host-config" ]] && LAMBDA_DIR="$(oc_kit_entry "$KIT")"
if [[ -n "$LAMBDA_DIR" ]]; then
  ok "compiled control-plane ($LANE) kit: $(basename "$LAMBDA_DIR")"
  for m in apply verify rollback; do
    [[ -f "$LAMBDA_DIR/$m.sh" ]] && ok "entrypoint $m.sh" || bad "missing $m.sh"
  done
  DECL="$(oc_lane_manifest_key "$LANE")"
  NFN="$(jq --arg d "$DECL" '(.[$d] // []) | length' "$KIT/manifest.json")"
  if [[ "$NFN" -eq 1 ]]; then ok "exactly 1 $LANE target"
  else bad "$NFN $DECL declared — one per kit, so each gets its own verify and rollback"; fi
elif [[ -f "$RECIPE" ]]; then
  ok "compiled recipe present"
  N="$(jq '.resources | length' "$RECIPE")"
  # The runner applies exactly one resource per run so each gets its own canary and its own
  # approval; catching this here beats discovering it after the lease is taken.
  if [[ "$N" -eq 1 ]]; then ok "exactly 1 compiled resource"
  else bad "$N compiled resources — split into one kit per resource"; fi
  for m in apply verify rollback clean; do
    [[ -f "$KIT/lib/compiled/$m.sh" ]] && ok "entrypoint $m.sh" || bad "missing $m.sh"
  done
else
  bad "no lib/compiled/recipe.json — this kit was never compiled (prose kit?)"
fi

if [[ -f "$KIT/manifest.json" ]]; then
  ST="$(jq -r '.status' "$KIT/manifest.json")"
  if [[ "$ST" == "READY" ]]; then ok "manifest status READY"
  else bad "manifest status $ST — only a READY kit may run unattended"; fi

  CONFIG_PATH="${OC_PATCH_CUSTOMER_CONFIG:-}"
  CONFIG_SHA="$(jq -r '.target_confirmation.customer_config_sha256 // ""' \
    "$KIT/manifest.json")"
  if [[ -z "$CONFIG_PATH" ]]; then
    bad "OC_PATCH_CUSTOMER_CONFIG must name the customer config.yml used to generate this kit"
  elif [[ ! -f "$CONFIG_PATH" || -L "$CONFIG_PATH" ]]; then
    bad "customer config is missing, not regular, or a symlink: $CONFIG_PATH"
  elif [[ "$(sha256sum "$CONFIG_PATH" | awk '{print $1}')" == "$CONFIG_SHA" ]]; then
    ok "customer config matches the manifest source hash"
  else
    bad "customer config changed after kit generation; regenerate and review the kits"
  fi

  HEADERS_PATH="${OC_PATCH_HTTP_HEADERS_FILE:-}"
  EXPECTED_HEADERS_SHA="$(jq -r \
    '.target_confirmation.authenticated_probe_headers_sha256 // ""' \
    "$KIT/manifest.json")"
  if [[ -z "$HEADERS_PATH" ]]; then
    bad "OC_PATCH_HTTP_HEADERS_FILE must name the authenticated probe headers confirmed by the operator"
  elif [[ ! -f "$HEADERS_PATH" || -L "$HEADERS_PATH" ]]; then
    bad "probe headers file is missing, not regular, or a symlink: $HEADERS_PATH"
  elif [[ ! "$EXPECTED_HEADERS_SHA" =~ ^[0-9a-f]{64}$ ]]; then
    bad "manifest has no valid authenticated probe headers hash"
  elif [[ "$(sha256sum "$HEADERS_PATH" | awk '{print $1}')" == "$EXPECTED_HEADERS_SHA" ]]; then
    ok "authenticated probe headers match the operator-confirmed manifest hash"
  else
    bad "probe headers changed after API confirmation; rediscover and review the kits"
  fi

  if jq -e --slurpfile env_file "$ENVJSON" '
      ($env_file[0]) as $env
      | .target_confirmation as $target
      | $target.entrypoint_kind == "explicit-rest-resources"
      and $target.proxy_resources_are_not_targets == true
      and $target.confirmed_api_id == $env.control_plane_api.id
      and $target.confirmed_stage == $env.control_plane_api.stage
      and $target.confirmed_client_url == $env.control_plane_api.configured_client_url
      and $target.authenticated_probe_headers_sha256
          == $env.control_plane_api.probe_headers_sha256
      and $env.control_plane_api.confirmed == true
      and $env.control_plane_api.entrypoint_kind == "explicit-rest-resources"
    ' "$KIT/manifest.json" >/dev/null 2>&1; then
    ok "operator-confirmed explicit REST API matches environment.json"
  else
    bad "manifest target and environment.json disagree, or target is a proxy resource"
  fi

  # Every generated lib file and every packaged runtime file is hash-bound. Checking only the
  # three lane entrypoints lets a tampered helper or driver reach the live runner.
  if jq -e '.kit_files | type == "object" and length > 0' "$KIT/manifest.json" >/dev/null 2>&1; then
    declared_count=0
    while IFS=$'\t' read -r rel want; do
      [[ -n "$rel" ]] || continue
      declared_count=$((declared_count + 1))
      case "$rel" in
        lib/*|runtime/scripts/*) ;;
        *) bad "kit_files entry escapes generated roots: $rel"; continue ;;
      esac
      if [[ -L "$KIT/$rel" ]]; then bad "generated file is a symlink: $rel"; continue; fi
      if [[ ! -f "$KIT/$rel" ]]; then bad "generated file missing: $rel"; continue; fi
      got="$(sha256sum "$KIT/$rel" | awk '{print $1}')"
      [[ "$got" == "$want" ]] && ok "generated file $rel matches kit_files" \
        || bad "generated file $rel hash $got != declared $want"
    done < <(jq -r '.kit_files | to_entries[] | [.key, .value.sha256] | @tsv' \
      "$KIT/manifest.json")
    actual_count="$(
      find "$KIT/lib" "$KIT/runtime/scripts" -type f 2>/dev/null |
        wc -l | tr -d ' '
    )"
    [[ "$actual_count" -eq "$declared_count" ]] \
      || bad "generated roots contain $actual_count file(s), kit_files declares $declared_count"
  else
    bad "manifest kit_files is empty — generated bytes are not hash-bound"
  fi
fi

say "== 2b. independent AI review receipt =="
set +e
REVIEW_CHECK="$(python3 "$HERE/review-kit.py" check "$KIT" 2>&1)"
REVIEW_RC=$?
set -e
if [[ "$REVIEW_RC" -eq 0 ]]; then
  ok "$REVIEW_CHECK"
else
  bad "${REVIEW_CHECK:-missing or invalid REVIEW.json/CLAUDE-REVIEW.txt}"
fi

say "== 3. artifact authenticity =="
# Prove the shipped bytes are the patch commit's bytes. A mis-packaged kit must be caught
# before it is installed, not after. A manifest with no `paths` at all is a prose-era shape,
# not "zero artifacts to check" — it must FAIL, never pass by vacuous truth.
if [[ -f "$KIT/manifest.json" ]]; then
  if ! jq -e '.paths | type == "object"' "$KIT/manifest.json" >/dev/null 2>&1; then
    bad "manifest has no .paths object — prose-era shape, nothing can be verified"
  else
    n_art=0
    while IFS=$'\t' read -r rel want; do
      [[ -n "$rel" ]] || continue
      n_art=$((n_art + 1))
      if [[ ! -f "$KIT/$rel" ]]; then bad "artifact $rel declared but absent"; continue; fi
      got="$(sha256sum "$KIT/$rel" | awk '{print $1}')"
      if [[ "$got" == "$want" ]]; then ok "artifact $rel matches patch_sha256"
      else bad "artifact $rel hash $got != declared $want"; fi
    done < <(jq -r '.paths | to_entries[]
      | select(.value.artifact != null)
      | [.value.artifact, .value.patch_sha256] | @tsv' "$KIT/manifest.json")
    if [[ "$n_art" -eq 0 ]]; then
      # A Lambda kit has no separate artifact FILES: the patched sources are in one generated,
      # hash-bound payload next to the scripts. So "zero artifacts" is correct there and a
      # defect anywhere else.
      if [[ -n "$LAMBDA_DIR" ]]; then
        case "$LANE" in
          tenantstats)
            backend="$(jq -r '.tenant_stats_backends[0].writer.function_name // ""' \
              "$KIT/manifest.json")"
            [[ -n "$backend" ]] \
              && ok "tenant statistics backend declared for $backend" \
              || bad "tenant statistics backend target is missing" ;;
          ddb)
            # A DDB kit ships no file: the whole change is a table SETTING, declared in the
            # manifest and baked into the recipe. Zero artifacts is correct here.
            tbl="$(jq -r '.ddb_settings[0].table // ""' "$KIT/manifest.json")"
            [[ -n "$tbl" ]] && ok "table setting declared for $tbl (no file artifact by design)" \
              || bad "DDB kit declares no table" ;;
          apigw)
            # An API Gateway kit ships no file either: the route is declared in the manifest and
            # created through the API, so zero artifacts is correct here too.
            rt="$(jq -r '.api_routes[0] | "\(.method) \(.path)"' "$KIT/manifest.json")"
            [[ "$rt" != "null null" ]] && ok "route declared: $rt (no file artifact by design)" \
              || bad "API Gateway kit declares no route" ;;
          *)
            overlay="$LAMBDA_DIR/payload/overlay.json"
            if [[ -f "$overlay" ]] && jq -e \
                '.sources | type == "object" and length > 0' "$overlay" >/dev/null 2>&1; then
              ok "sources present in the hash-bound compiled payload (Lambda kit)"
            else
              bad "Lambda kit has neither shipped artifacts nor a compiled source payload"
            fi ;;
        esac
      else
        bad "no shipped artifact in .paths — nothing would be installed"
      fi
    fi
  fi
fi

say "== 4. target environment identity =="
REGION="$(jq -r '.region // ""' "$ENVJSON")"
ACCOUNT="$(jq -r '.account // "" | tostring' "$ENVJSON")"
[[ -n "$REGION" ]] && ok "region $REGION" || bad "no .region in environment.json"
[[ "$ACCOUNT" =~ ^[0-9]{12}$ ]] && ok "account $ACCOUNT" || bad "no valid .account"
if [[ -n "$REGION" && -n "$ACCOUNT" ]]; then
  LIVE="$(aws sts get-caller-identity --region "$REGION" --query Account --output text 2>/dev/null || true)"
  if [[ "$LIVE" == "$ACCOUNT" ]]; then ok "live credentials point at $ACCOUNT"
  else bad "live credentials are for '${LIVE:-<unreadable>}', environment says $ACCOUNT"; fi
fi

say "== 5. target reachability =="
# A control-plane kit has no host fleet: demanding a host snapshot would reject a perfectly
# valid Lambda-only environment, and passing without checking the Lambda would let the run
# discover a missing permission after the interview. Check what this kit actually touches.
if [[ "$LANE" == "tenantstats" ]]; then
  API_FN="$(jq -r '.lambda_link.function // ""' "$ENVJSON")"
  TBL="$(jq -r '.tenant_stats_backends[0].table.name' "$KIT/manifest.json")"
  if aws lambda get-function-configuration --region "$REGION" \
      --function-name "$API_FN" --query FunctionName --output text >/dev/null 2>&1; then
    ok "source API Lambda contract is readable"
  else
    bad "cannot read openclaw-api to cross-check writer inputs"
  fi
  set +e
  TABLE_READ="$(aws dynamodb describe-table --region "$REGION" --table-name "$TBL" \
    --query 'Table.TableStatus' --output text 2>&1)"
  TABLE_READ_RC=$?
  set -e
  if [[ "$TABLE_READ_RC" -eq 0 ]]; then
    ok "table $TBL exists; apply will verify its exact schema"
  elif grep -q 'ResourceNotFoundException' <<< "$TABLE_READ"; then
    ok "table $TBL is absent; apply will create it before the writer"
  else
    bad "cannot determine whether table $TBL exists: ${TABLE_READ:0:160}"
  fi
  mapfile -t HOSTS < <(printf '')
elif [[ "$LANE" == "ddb" ]]; then
  # A DDB kit's target is a table setting, so reachability means "can I read the setting this
  # kit owns". Checking a Lambda here would report a null function and fail a valid kit.
  TBL="$(jq -r '.ddb_settings[0].table' "$KIT/manifest.json")"
  SET="$(jq -r '.ddb_settings[0].setting' "$KIT/manifest.json")"
  if aws dynamodb describe-table --region "$REGION" --table-name "$TBL"       --query 'Table.TableStatus' --output text >/dev/null 2>&1; then
    ok "table $TBL readable"
  else bad "cannot read table $TBL — wrong account/region, or it does not exist"; fi
  case "$SET" in
    ttl)  READ_OK="$(aws dynamodb describe-time-to-live --region "$REGION"             --table-name "$TBL" --query 'TimeToLiveDescription.TimeToLiveStatus'             --output text 2>/dev/null || true)" ;;
    pitr) READ_OK="$(aws dynamodb describe-continuous-backups --region "$REGION"             --table-name "$TBL"             --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus'             --output text 2>/dev/null || true)" ;;
    *)    READ_OK="$(aws dynamodb describe-table --region "$REGION" --table-name "$TBL"             --query 'Table.DeletionProtectionEnabled' --output text 2>/dev/null || true)" ;;
  esac
  # The current value is what rollback will target, so being unable to read it now means the
  # run could not restore anything later.
  [[ -n "$READ_OK" ]] && ok "current $SET = $READ_OK (rollback target)"     || bad "cannot read $SET on $TBL — rollback would have no baseline"
  mapfile -t HOSTS < <(printf '')
elif [[ "$LANE" == "ddbnew" ]]; then
  # A create-only kit has no rollback target to read: the table is not there yet. What must be
  # true is that the name is free, or already holds the table this patch means. A name taken by a
  # table with a DIFFERENT key schema is fatal and must be known before the run starts, because a
  # primary key cannot be changed in place.
  TBL="$(jq -r '.ddb_tables[0].table' "$KIT/manifest.json")"
  set +e
  TABLE_READ="$(aws dynamodb describe-table --region "$REGION" --table-name "$TBL" \
    --query 'Table.TableStatus' --output text 2>&1)"
  TABLE_READ_RC=$?
  set -e
  if [[ "$TABLE_READ_RC" -eq 0 ]]; then
    LIVE_ST="$TABLE_READ"
    ok "table $TBL already exists ($LIVE_ST) — apply will verify the key schema and SKIP"
  elif grep -q 'ResourceNotFoundException' <<< "$TABLE_READ"; then
    LIVE_ST=""
    ok "table $TBL does not exist yet (apply will create it)"
  else
    LIVE_ST=""
    bad "cannot determine whether table $TBL exists: ${TABLE_READ:0:160}"
  fi
  say "  NOTE  this lane is CREATE-ONLY: rollback refuses, because undoing a table means"
  say "        deleting it and whatever has been written since."
  mapfile -t HOSTS < <(printf '')
elif [[ "$LANE" == "apigw" ]]; then
  # Reachability for a route change means: the API exists, the stage exists, and the stage is on
  # a deployment. That last one is the whole rollback — a deployment is a snapshot of the
  # configuration, so returning the stage to the recorded deployment restores the old routing.
  # An undeployed stage has nothing to go back to, which must be known before the first write.
  AID="$(jq -r '.api_routes[0].api_id' "$KIT/manifest.json")"
  STG="$(jq -r '.api_routes[0].stage' "$KIT/manifest.json")"
  TFN="$(jq -r '.api_routes[0].target_function' "$KIT/manifest.json")"
  if aws apigateway get-rest-api --region "$REGION" --rest-api-id "$AID"       --query id --output text >/dev/null 2>&1; then
    ok "rest api $AID readable"
  else bad "cannot read rest api $AID — wrong account/region, or it does not exist"; fi
  BASE_DEP="$(aws apigateway get-stage --region "$REGION" --rest-api-id "$AID"     --stage-name "$STG" --query deploymentId --output text 2>/dev/null || true)"
  if [[ -n "$BASE_DEP" && "$BASE_DEP" != "None" ]]; then
    ok "stage $STG is on deployment $BASE_DEP (rollback target)"
  else bad "stage $STG has no deployment — nothing to roll back to"; fi
  if aws lambda get-function-configuration --region "$REGION" --function-name "$TFN"       --query FunctionName --output text >/dev/null 2>&1; then
    ok "target lambda $TFN readable"
  else bad "cannot read target lambda $TFN — the route would 502"; fi
  mapfile -t HOSTS < <(printf '')
elif [[ "$LANE" == "lambda" ]]; then
  FN="$(jq -r '.lambda_functions[0].function_name' "$KIT/manifest.json")"
  AL="$(jq -r '.lambda_functions[0].alias // "live"' "$KIT/manifest.json")"
  if aws lambda get-function-configuration --region "$REGION" --function-name "$FN"       --query CodeSha256 --output text >/dev/null 2>&1; then
    ok "lambda $FN readable"
  else bad "cannot read lambda $FN"; fi
  if aws lambda get-alias --region "$REGION" --function-name "$FN" --name "$AL"       --query FunctionVersion --output text >/dev/null 2>&1; then
    ok "alias $AL exists"
  else bad "alias $AL not found on $FN — the apply would have nothing to move"; fi
  # get-function returns the presigned Code.Location the overlay downloads; without it the
  # overlay cannot start, and that is better known now than after the backup anchor exists.
  if aws lambda get-function --region "$REGION" --function-name "$FN"       --query Code.Location --output text >/dev/null 2>&1; then
    ok "package download URL obtainable (overlay can start)"
  else bad "cannot obtain Code.Location — lambda:GetFunction missing?"; fi
  # The apply's safety story is "update $LATEST, verify it, THEN move the alias, so live traffic
  # stays on the old version until the new one is proven". That holds only if every caller goes
  # through the alias. Measured on this testbed: the private REST API integrates ANY / and
  # ANY /{proxy+} against the UNQUALIFIED function ARN, so 100% of its traffic hits $LATEST the
  # instant it is updated — before the verify. That is not a reason to refuse; it is a reason the
  # operator must know which of the two stories applies to THIS environment.
  # `|| true` would make "could not check" indistinguishable from "nothing found", which is the
  # exact confusion this check exists to remove. Keep the exit status.
  set +e
  UNQ_ROUTES="$(python3 "$(dirname "$0")/find-unqualified-routes.py" "$REGION" "$FN" 2>&1)"
  UNQ_RC=$?
  set -e
  if [[ "$UNQ_RC" -ne 0 ]]; then
    say "  NOTE  could not enumerate API routes: ${UNQ_ROUTES:0:160}"
    say "        whether all API traffic is alias-gated is UNVERIFIED for this run"
  elif [[ -z "$UNQ_ROUTES" ]]; then
    ok "every API route reaches $FN through an alias (the alias gate covers all API traffic)"
  else
    while read -r route; do
      [[ -n "$route" ]] || continue
      say "  NOTE  $route -> \$LATEST (NOT alias-gated)"
    done <<< "$UNQ_ROUTES"
    say "  NOTE  updating \$LATEST exposes the route(s) above to the new code BEFORE the verify."
    say "        The alias gate still protects alias-bound callers, and rollback reverts both."
  fi
  mapfile -t HOSTS < <(printf '')
elif [[ "$(jq -r '.hosts.instance_ids | length' "$ENVJSON" 2>/dev/null || echo 0)" -eq 0 ]]; then
  bad "no .hosts.instance_ids — run discover-env.sh first"
  mapfile -t HOSTS < <(printf '')
else
  mapfile -t HOSTS < <(jq -r '.hosts.instance_ids[]? // empty' "$ENVJSON")
  ok "${#HOSTS[@]} host(s) in the snapshot"
  ONLINE="$(aws ssm describe-instance-information --region "$REGION" \
    --filters "Key=InstanceIds,Values=$(IFS=,; echo "${HOSTS[*]}")" \
    --query 'length(InstanceInformationList[?PingStatus==`Online`])' --output text 2>/dev/null || echo 0)"
  # Every snapshot host must be reachable. A half-reachable fleet means the canary could pass
  # while some hosts silently never get the patch.
  if [[ "$ONLINE" == "${#HOSTS[@]}" ]]; then ok "all ${#HOSTS[@]} host(s) Online in SSM"
  else bad "only $ONLINE of ${#HOSTS[@]} host(s) Online in SSM"; fi
fi

say "== 6. required render bindings =="
# Only meaningful when .paths exists. Without it, jq iterates null and the loop body never
# runs, so an empty `missing` array would report PASS for a manifest that declares nothing —
# vacuous-truth green, the same class of bug as claiming coverage without checking.
if [[ -f "$KIT/manifest.json" ]]; then
  if ! jq -e '.paths | type == "object"' "$KIT/manifest.json" >/dev/null 2>&1; then
    bad "cannot check render bindings: manifest has no .paths object"
  else
    missing=()
    while read -r name; do
      [[ -n "$name" ]] || continue
      v="$(jq -r --arg n "$name" '.bindings[$n] // ""' "$ENVJSON")"
      [[ -n "$v" ]] || missing+=("$name")
    done < <(jq -r '.paths[].render.required_bindings[]? // empty' "$KIT/manifest.json" | sort -u)
    if [[ "${#missing[@]}" -eq 0 ]]; then ok "all render bindings present"
    else bad "environment.json .bindings missing: ${missing[*]}"; fi
  fi
fi

say "== 7. write permissions the run needs =="
if [[ "$LANE" == "tenantstats" ]]; then
  ACTIONS=(
    dynamodb:CreateTable dynamodb:DescribeTable dynamodb:ListTagsOfResource
    dynamodb:DescribeContinuousBackups dynamodb:UpdateContinuousBackups dynamodb:GetItem
    iam:GetRole iam:CreateRole iam:ListRoleTags iam:GetRolePolicy iam:PutRolePolicy iam:TagRole
    lambda:GetFunction lambda:GetFunctionConcurrency lambda:CreateFunction
    lambda:PutFunctionConcurrency lambda:InvokeFunction lambda:AddPermission
    lambda:RemovePermission lambda:GetPolicy lambda:TagResource
    events:DescribeRule events:ListTagsForResource events:ListTargetsByRule
    events:PutRule events:PutTargets events:EnableRule events:DisableRule
    events:RemoveTargets events:TagResource
  )
  SIM="$(aws iam simulate-principal-policy --policy-source-arn "$(principal_arn)" \
    --action-names "${ACTIONS[@]}" \
    --query 'EvaluationResults[].[EvalActionName,EvalDecision]' \
    --output text 2>/dev/null || true)"
  if [[ -z "$SIM" ]]; then
    bad "iam:SimulatePrincipalPolicy unavailable — backend permissions unverified"
  else
    for act in "${ACTIONS[@]}"; do
      dec="$(awk -F '\t' -v wanted="$act" '$1 == wanted { print $2; exit }' <<< "$SIM")"
      if [[ "$dec" == "allowed" ]]; then ok "$act allowed"
      elif [[ -z "$dec" ]]; then bad "$act has no simulation result"
      else bad "$act is $dec — backend apply would fail partway"; fi
    done
  fi
  verdict
elif [[ "$LANE" == "ddbnew" ]]; then
  TBL="$(jq -r '.ddb_tables[0].table' "$KIT/manifest.json")"
  ACTIONS=(dynamodb:CreateTable dynamodb:DescribeTable dynamodb:UpdateContinuousBackups)
  SIM="$(aws iam simulate-principal-policy --policy-source-arn "$(principal_arn)"     --action-names "${ACTIONS[@]}"     --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text 2>/dev/null || true)"
  if [[ -z "$SIM" ]]; then
    bad "iam:SimulatePrincipalPolicy unavailable — DDB create permissions unverified"
  else
    for act in "${ACTIONS[@]}"; do
      dec="$(awk -F '\t' -v wanted="$act" '$1 == wanted { print $2; exit }' <<< "$SIM")"
      if [[ "$dec" == "allowed" ]]; then ok "$act allowed"
      elif [[ -z "$dec" ]]; then bad "$act has no simulation result on $TBL"
      else bad "$act is $dec on $TBL — apply would fail partway"; fi
    done
  fi
  verdict
elif [[ "$LANE" == "apigw" ]]; then
  AID="$(jq -r '.api_routes[0].api_id' "$KIT/manifest.json")"
  # Exactly the calls apply makes: create the resource chain, put the method and integration,
  # deploy, and (for rollback) repoint the stage. lambda:AddPermission is included because the
  # route 502s without it.
  ACTIONS=(apigateway:GET apigateway:POST apigateway:PUT apigateway:PATCH
           lambda:AddPermission lambda:GetPolicy)
  SIM="$(aws iam simulate-principal-policy --policy-source-arn "$(principal_arn)"     --action-names "${ACTIONS[@]}"     --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text 2>/dev/null || true)"
  if [[ -z "$SIM" ]]; then
    bad "iam:SimulatePrincipalPolicy unavailable — API route permissions unverified"
  else
    for act in "${ACTIONS[@]}"; do
      dec="$(awk -F '\t' -v wanted="$act" '$1 == wanted { print $2; exit }' <<< "$SIM")"
      if [[ "$dec" == "allowed" ]]; then ok "$act allowed"
      elif [[ -z "$dec" ]]; then bad "$act has no simulation result on $AID"
      else bad "$act is $dec on $AID — apply would fail partway"; fi
    done
  fi
  verdict
elif [[ "$LANE" == "ddb" ]]; then
  TBL="$(jq -r '.ddb_settings[0].table' "$KIT/manifest.json")"
  SET="$(jq -r '.ddb_settings[0].setting' "$KIT/manifest.json")"
  case "$SET" in
    ttl)  ACTIONS=(dynamodb:DescribeTimeToLive dynamodb:UpdateTimeToLive) ;;
    pitr) ACTIONS=(dynamodb:DescribeContinuousBackups dynamodb:UpdateContinuousBackups) ;;
    *)    ACTIONS=(dynamodb:DescribeTable dynamodb:UpdateTable) ;;
  esac
  SIM="$(aws iam simulate-principal-policy --policy-source-arn "$(principal_arn)"     --action-names "${ACTIONS[@]}"     --resource-arns "arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${TBL}"     --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text 2>/dev/null || true)"
  if [[ -z "$SIM" ]]; then
    bad "iam:SimulatePrincipalPolicy unavailable — DDB setting permissions unverified"
  else
    for act in "${ACTIONS[@]}"; do
      dec="$(awk -F '\t' -v wanted="$act" '$1 == wanted { print $2; exit }' <<< "$SIM")"
      if [[ "$dec" == "allowed" ]]; then ok "$act allowed"
      elif [[ -z "$dec" ]]; then bad "$act has no simulation result on $TBL"
      else bad "$act is $dec on $TBL — apply would fail partway"; fi
    done
  fi
  verdict
elif [[ "$LANE" == "lambda" ]]; then
  # Probing these writes for real would mutate the function, so derive the exact
  # permission set from the manifest and ask IAM. Missing simulation output is not
  # evidence of access: unattended apply would otherwise discover a denial after a
  # backup or configuration write.
  for t in curl unzip zip; do
    command -v "$t" >/dev/null && ok "overlay tool $t" || bad "overlay needs $t"
  done
  CALLER_ARN="$(principal_arn)"
  if [[ -z "$CALLER_ARN" ]]; then
    bad "cannot read the caller identity — cannot check write permissions"
  else
    FN_ARN="arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FN}"
    LAMBDA_ACTIONS=(
      lambda:GetFunction
      lambda:InvokeFunction
      lambda:PublishVersion
      lambda:UpdateAlias
      lambda:UpdateFunctionCode
    )
    if jq -e '
        (.lambda_functions[0].environment_updates // {} | length) > 0
        or (.lambda_functions[0].generated_environment // {} | length) > 0
      ' "$KIT/manifest.json" >/dev/null; then
      LAMBDA_ACTIONS+=(lambda:UpdateFunctionConfiguration)
    fi
    SIM="$(aws iam simulate-principal-policy --policy-source-arn "$CALLER_ARN" \
      --action-names "${LAMBDA_ACTIONS[@]}" --resource-arns "$FN_ARN" \
      --query 'EvaluationResults[].[EvalActionName,EvalDecision]' \
      --output text 2>/dev/null || true)"
    if [[ -z "$SIM" ]]; then
      bad "iam:SimulatePrincipalPolicy unavailable — Lambda write permissions unverified"
    else
      for act in "${LAMBDA_ACTIONS[@]}"; do
        dec="$(awk -F '\t' -v wanted="$act" '$1 == wanted { print $2; exit }' <<< "$SIM")"
        if [[ "$dec" == "allowed" ]]; then ok "$act allowed"
        elif [[ -z "$dec" ]]; then bad "$act has no simulation result on $FN"
        else bad "$act is $dec on $FN — apply would fail partway"; fi
      done
    fi

    if jq -e '(.lambda_functions[0].iam_read_tables // [] | length) > 0' \
        "$KIT/manifest.json" >/dev/null; then
      ROLE_ARN="$(aws lambda get-function-configuration --region "$REGION" \
        --function-name "$FN" --query Role --output text 2>/dev/null || true)"
      if [[ -z "$ROLE_ARN" || "$ROLE_ARN" == "None" ]]; then
        bad "cannot resolve the Lambda execution role for IAM policy checks"
      else
        IAM_ACTIONS=(iam:GetRolePolicy iam:PutRolePolicy iam:DeleteRolePolicy)
        IAM_SIM="$(aws iam simulate-principal-policy --policy-source-arn "$CALLER_ARN" \
          --action-names "${IAM_ACTIONS[@]}" --resource-arns "$ROLE_ARN" \
          --query 'EvaluationResults[].[EvalActionName,EvalDecision]' \
          --output text 2>/dev/null || true)"
        if [[ -z "$IAM_SIM" ]]; then
          bad "iam:SimulatePrincipalPolicy unavailable — role policy permissions unverified"
        else
          for act in "${IAM_ACTIONS[@]}"; do
            dec="$(awk -F '\t' -v wanted="$act" \
              '$1 == wanted { print $2; exit }' <<< "$IAM_SIM")"
            if [[ "$dec" == "allowed" ]]; then ok "$act allowed"
            elif [[ -z "$dec" ]]; then bad "$act has no simulation result on $ROLE_ARN"
            else bad "$act is $dec on $ROLE_ARN — apply or rollback would fail partway"; fi
          done
        fi
      fi
    fi
  fi
  verdict
fi
BUCKET="$(jq -r '.assets_bucket // .bindings.ASSETS_BUCKET // ""' "$ENVJSON")"
if [[ -z "$BUCKET" ]]; then bad "no .assets_bucket — the lease and staged scripts need it"
else
  # HeadBucket is read-only but proves the principal can address the bucket at all; the
  # conditional-write lease is exercised for real by the runner, not faked here.
  if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
    ok "assets bucket $BUCKET reachable"
  else bad "cannot head bucket $BUCKET"; fi
fi
if aws ssm describe-instance-information --region "$REGION" --max-items 1 >/dev/null 2>&1; then
  ok "ssm:DescribeInstanceInformation allowed"
else bad "no ssm:DescribeInstanceInformation — send-command will fail later"; fi

# The host lane's WRITE permissions, probed without writing. ssm:SendCommand and the
# conditional-write S3 calls have no dry-run that is both safe and meaningful, so ask IAM.
# Failing here costs a rerun; failing mid-rollout leaves a fleet half-patched.
CALLER_ARN="$(principal_arn)"
if [[ -z "$CALLER_ARN" ]]; then
  bad "cannot read the caller identity — cannot check write permissions"
else
  SIM="$(aws iam simulate-principal-policy --policy-source-arn "$CALLER_ARN"     --action-names ssm:SendCommand ssm:ListCommandInvocations                    s3:PutObject s3:GetObject s3:DeleteObject     --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text 2>/dev/null || true)"
  if [[ -z "$SIM" ]]; then
    bad "iam:SimulatePrincipalPolicy unavailable — host rollout permissions unverified"
  else
    HOST_ACTIONS=(ssm:SendCommand ssm:ListCommandInvocations
      s3:PutObject s3:GetObject s3:DeleteObject)
    for act in "${HOST_ACTIONS[@]}"; do
      dec="$(awk -F '\t' -v wanted="$act" '$1 == wanted { print $2; exit }' <<< "$SIM")"
      if [[ "$dec" == "allowed" ]]; then ok "$act allowed"
      elif [[ -z "$dec" ]]; then bad "$act has no simulation result"
      else bad "$act is $dec — the rollout would fail partway"; fi
    done
  fi
fi

verdict
