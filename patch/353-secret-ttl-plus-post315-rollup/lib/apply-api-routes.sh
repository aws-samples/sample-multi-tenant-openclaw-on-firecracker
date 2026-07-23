#!/usr/bin/env bash
# Install the API Gateway routes added by patch 353 without a CloudFormation deploy.
# Existing /images is the source of truth for method auth and Lambda alias integration.
set -euo pipefail

CONFIRM="APPLY"
STATE_DIR="${OC_APPLY_API_STATE_DIR:-$HOME/.oc-apply-api-routes}"
TARGET_PATHS=(
  "/list_image_versions"
  "/hosts/{instance_id}/pull-image-progress"
  "/hosts/{instance_id}/copy-file-from-s3"
)
TARGET_PARTS=("list_image_versions" "pull-image-progress" "copy-file-from-s3")
TARGET_METHODS=("GET" "GET" "POST")

_usage() {
  echo "usage: apply-api-routes.sh plan|apply|verify|rollback <rest-api-id> <stage> <region>" >&2
}
_need() { command -v "$1" >/dev/null || { echo "FATAL: need '$1' on PATH" >&2; exit 1; }; }
_gate() {
  echo
  echo ">>> $1"
  printf "    type '%s' to proceed (else abort): " "$CONFIRM"
  read -r answer
  [ "$answer" = "$CONFIRM" ] || { echo "aborted"; exit 3; }
}
_q() { aws "$@" --region "$REGION" --output json; }
_statefile() { echo "$STATE_DIR/$API.$STAGE.json"; }
_rid() {
  _q apigateway get-resources --rest-api-id "$API" \
    | jq -r --arg path "$1" '[.items[] | select(.path == $path)] | if length == 1 then .[0].id else empty end'
}
_state_update() {
  local filter="$1"; shift
  jq "$@" "$filter" "$ST" > "$ST.tmp"
  mv "$ST.tmp" "$ST"
  chmod 600 "$ST"
}
_assert_targets_absent() {
  local path
  for path in "${TARGET_PATHS[@]}"; do
    [ -z "$(_rid "$path")" ] || {
      echo "FATAL: target resource '$path' already exists; refusing to overwrite it." >&2
      echo "       Run verify instead, or remove the partial route manually after review." >&2
      exit 2
    }
  done
}

_preflight() {
  ROOT_ID="$(_rid "/")"
  HOST_ID="$(_rid "/hosts/{instance_id}")"
  SOURCE_ID="$(_rid "/images")"
  [ -n "$ROOT_ID" ] || { echo "FATAL: API has no root resource" >&2; exit 2; }
  [ -n "$HOST_ID" ] || { echo "FATAL: API has no /hosts/{instance_id} parent resource" >&2; exit 2; }
  [ -n "$SOURCE_ID" ] || { echo "FATAL: API has no /images template resource" >&2; exit 2; }

  SOURCE_METHOD="$(_q apigateway get-method --rest-api-id "$API" \
    --resource-id "$SOURCE_ID" --http-method GET)"
  SOURCE_INTEGRATION="$(_q apigateway get-integration --rest-api-id "$API" \
    --resource-id "$SOURCE_ID" --http-method GET)"
  [ "$(printf '%s' "$SOURCE_INTEGRATION" | jq -r .type)" = "AWS_PROXY" ] || {
    echo "FATAL: /images GET is not an AWS_PROXY integration; refusing to invent a different route shape" >&2
    exit 2
  }
  [ "$(printf '%s' "$SOURCE_INTEGRATION" | jq -r .httpMethod)" = "POST" ] || {
    echo "FATAL: /images integration HTTP method is not POST" >&2
    exit 2
  }
  SOURCE_URI="$(printf '%s' "$SOURCE_INTEGRATION" | jq -r '.uri // empty')"
  [ -n "$SOURCE_URI" ] || { echo "FATAL: /images integration has no URI" >&2; exit 2; }
}

_print_plan() {
  local i path state
  echo "API=$API stage=$STAGE region=$REGION"
  echo "template: GET /images"
  printf '%s' "$SOURCE_METHOD" | jq '{authorizationType,apiKeyRequired,authorizerId,authorizationScopes}'
  printf '%s' "$SOURCE_INTEGRATION" | jq '{type,httpMethod,uri,timeoutInMillis}'
  for i in "${!TARGET_PATHS[@]}"; do
    path="${TARGET_PATHS[$i]}"
    if [ -n "$(_rid "$path")" ]; then state="PRESENT"; else state="MISSING"; fi
    echo "  $state  ${TARGET_METHODS[$i]} $path (+ OPTIONS CORS)"
  done
}

_put_main_method() {
  local rid="$1" method="$2" method_input integration_input
  method_input="$(printf '%s' "$SOURCE_METHOD" | jq -c \
    --arg api "$API" --arg rid "$rid" --arg method "$method" '
      {
        restApiId:$api, resourceId:$rid, httpMethod:$method,
        authorizationType:.authorizationType,
        apiKeyRequired:(.apiKeyRequired // false)
      }
      + (if .authorizerId then {authorizerId:.authorizerId} else {} end)
      + (if ((.authorizationScopes // []) | length) > 0
         then {authorizationScopes:.authorizationScopes} else {} end)
    ')"
  _q apigateway put-method --cli-input-json "$method_input" >/dev/null

  integration_input="$(printf '%s' "$SOURCE_INTEGRATION" | jq -c \
    --arg api "$API" --arg rid "$rid" --arg method "$method" '
      {
        restApiId:$api, resourceId:$rid, httpMethod:$method,
        type:.type, integrationHttpMethod:.httpMethod, uri:.uri,
        passthroughBehavior:(.passthroughBehavior // "WHEN_NO_MATCH"),
        timeoutInMillis:(.timeoutInMillis // 29000)
      }
      + (if .credentials then {credentials:.credentials} else {} end)
      + (if .contentHandling then {contentHandling:.contentHandling} else {} end)
    ')"
  _q apigateway put-integration --cli-input-json "$integration_input" >/dev/null
}

_put_cors() {
  local rid="$1" input
  input="$(jq -nc --arg api "$API" --arg rid "$rid" \
    '{restApiId:$api,resourceId:$rid,httpMethod:"OPTIONS",authorizationType:"NONE",apiKeyRequired:false}')"
  _q apigateway put-method --cli-input-json "$input" >/dev/null

  input="$(jq -nc --arg api "$API" --arg rid "$rid" '{
    restApiId:$api,resourceId:$rid,httpMethod:"OPTIONS",type:"MOCK",
    requestTemplates:{"application/json":"{ statusCode: 200 }"},
    passthroughBehavior:"WHEN_NO_MATCH",timeoutInMillis:29000
  }')"
  _q apigateway put-integration --cli-input-json "$input" >/dev/null

  input="$(jq -nc --arg api "$API" --arg rid "$rid" '{
    restApiId:$api,resourceId:$rid,httpMethod:"OPTIONS",statusCode:"204",
    responseParameters:{
      "method.response.header.Access-Control-Allow-Headers":true,
      "method.response.header.Access-Control-Allow-Methods":true,
      "method.response.header.Access-Control-Allow-Origin":true
    }
  }')"
  _q apigateway put-method-response --cli-input-json "$input" >/dev/null

  input="$(jq -nc --arg api "$API" --arg rid "$rid" '{
    restApiId:$api,resourceId:$rid,httpMethod:"OPTIONS",statusCode:"204",
    responseParameters:{
      "method.response.header.Access-Control-Allow-Headers":"\u0027Content-Type,x-api-key,Authorization\u0027",
      "method.response.header.Access-Control-Allow-Methods":"\u0027OPTIONS,GET,PUT,POST,DELETE,PATCH,HEAD\u0027",
      "method.response.header.Access-Control-Allow-Origin":"\u0027*\u0027"
    }
  }')"
  _q apigateway put-integration-response --cli-input-json "$input" >/dev/null
}

_verify() {
  local i path method rid got_method got_integration got_options got_cors expected_auth got_auth
  expected_auth="$(printf '%s' "$SOURCE_METHOD" \
    | jq -c '{authorizationType,apiKeyRequired,authorizerId,authorizationScopes}')"
  for i in "${!TARGET_PATHS[@]}"; do
    path="${TARGET_PATHS[$i]}"; method="${TARGET_METHODS[$i]}"; rid="$(_rid "$path")"
    [ -n "$rid" ] || { echo "FATAL: missing resource $path" >&2; exit 2; }
    got_method="$(_q apigateway get-method --rest-api-id "$API" --resource-id "$rid" --http-method "$method")"
    got_auth="$(printf '%s' "$got_method" \
      | jq -c '{authorizationType,apiKeyRequired,authorizerId,authorizationScopes}')"
    [ "$got_auth" = "$expected_auth" ] || {
      echo "FATAL: $method $path auth differs from GET /images" >&2
      exit 2
    }
    got_integration="$(_q apigateway get-integration --rest-api-id "$API" \
      --resource-id "$rid" --http-method "$method")"
    [ "$(printf '%s' "$got_integration" | jq -r .type)" = "AWS_PROXY" ] \
      && [ "$(printf '%s' "$got_integration" | jq -r .httpMethod)" = "POST" ] \
      && [ "$(printf '%s' "$got_integration" | jq -r .uri)" = "$SOURCE_URI" ] || {
        echo "FATAL: $method $path integration differs from GET /images" >&2
        exit 2
      }
    got_options="$(_q apigateway get-method --rest-api-id "$API" \
      --resource-id "$rid" --http-method OPTIONS)"
    got_cors="$(_q apigateway get-integration --rest-api-id "$API" \
      --resource-id "$rid" --http-method OPTIONS)"
    [ "$(printf '%s' "$got_options" | jq -r '.authorizationType')" = "NONE" ] \
      && [ "$(printf '%s' "$got_options" | jq -r '.apiKeyRequired')" = "false" ] \
      && [ "$(printf '%s' "$got_options" | jq -r '.methodResponses["204"].statusCode')" = "204" ] \
      && [ "$(printf '%s' "$got_cors" | jq -r '.type')" = "MOCK" ] \
      && [ "$(printf '%s' "$got_cors" | jq -r '.integrationResponses["204"].statusCode')" = "204" ] || {
        echo "FATAL: OPTIONS $path is not the expected unauthenticated MOCK -> 204 CORS route" >&2
        exit 2
      }
    echo "PASS: $method $path + OPTIONS -> $SOURCE_URI"
  done

  CURRENT_DEPLOY="$(_q apigateway get-stage --rest-api-id "$API" --stage-name "$STAGE" \
    | jq -r '.deploymentId // empty')"
  [ -n "$CURRENT_DEPLOY" ] || { echo "FATAL: stage '$STAGE' has no deployment" >&2; exit 2; }
  ST="$(_statefile)"
  if [ -f "$ST" ]; then
    EXPECTED_DEPLOY="$(jq -r '.new_deployment // empty' "$ST")"
    [ -z "$EXPECTED_DEPLOY" ] || [ "$CURRENT_DEPLOY" = "$EXPECTED_DEPLOY" ] || {
      echo "FATAL: stage drifted to deployment $CURRENT_DEPLOY, expected $EXPECTED_DEPLOY" >&2
      exit 2
    }
  fi
  echo "PASS: stage '$STAGE' deployment=$CURRENT_DEPLOY"
}

_need aws
_need jq
[ $# -eq 4 ] || { _usage; exit 2; }
CMD="$1"; API="$2"; STAGE="$3"; REGION="$4"

case "$CMD" in
  plan)
    _preflight
    _print_plan
    ;;
  apply)
    _preflight
    _print_plan
    _assert_targets_absent
    _gate "create the 3 patch-353 routes on API '$API' and deploy stage '$STAGE'"
    _preflight
    _assert_targets_absent
    mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
    ST="$(_statefile)"
    [ ! -e "$ST" ] || { echo "FATAL: state already exists at $ST; rollback or archive it first" >&2; exit 2; }
    PREVIOUS_DEPLOY="$(_q apigateway get-stage --rest-api-id "$API" --stage-name "$STAGE" \
      | jq -r '.deploymentId // empty')"
    [ -n "$PREVIOUS_DEPLOY" ] || { echo "FATAL: stage '$STAGE' has no deployment" >&2; exit 2; }
    jq -n --arg api "$API" --arg stage "$STAGE" --arg region "$REGION" --arg prev "$PREVIOUS_DEPLOY" \
      '{api:$api,stage:$stage,region:$region,previous_deployment:$prev,new_deployment:null,
        created_resources:[],rolled_back:false}' > "$ST"
    chmod 600 "$ST"

    for i in "${!TARGET_PATHS[@]}"; do
      path="${TARGET_PATHS[$i]}"; part="${TARGET_PARTS[$i]}"; method="${TARGET_METHODS[$i]}"
      if [ "$i" -eq 0 ]; then parent="$ROOT_ID"; else parent="$HOST_ID"; fi
      rid="$(_q apigateway create-resource --rest-api-id "$API" --parent-id "$parent" \
        --path-part "$part" | jq -r .id)"
      [ -n "$rid" ] && [ "$rid" != "null" ] || { echo "FATAL: create-resource returned no id for $path" >&2; exit 2; }
      # shellcheck disable=SC2016  # jq variables, not shell variables
      _state_update '.created_resources += [{path:$path,id:$id}]' --arg path "$path" --arg id "$rid"
      _put_main_method "$rid" "$method"
      _put_cors "$rid"
      echo "CREATED: $method $path + OPTIONS"
    done

    NEW_DEPLOY="$(_q apigateway create-deployment --rest-api-id "$API" \
      --description "patch 353 hot routes" | jq -r .id)"
    [ -n "$NEW_DEPLOY" ] && [ "$NEW_DEPLOY" != "null" ] || {
      echo "FATAL: create-deployment returned no id; run rollback" >&2
      exit 2
    }
    # shellcheck disable=SC2016  # jq variable, not a shell variable
    _state_update '.new_deployment=$dep' --arg dep "$NEW_DEPLOY"
    _q apigateway update-stage --rest-api-id "$API" --stage-name "$STAGE" \
      --patch-operations "op=replace,path=/deploymentId,value=$NEW_DEPLOY" >/dev/null
    _verify
    echo "OK: routes deployed; rollback state is $ST"
    ;;
  verify)
    _preflight
    _verify
    ;;
  rollback)
    ST="$(_statefile)"
    [ -f "$ST" ] || { echo "FATAL: no state $ST" >&2; exit 2; }
    [ "$(jq -r .rolled_back "$ST")" != "true" ] || { echo "FATAL: state is already rolled back" >&2; exit 2; }
    PREVIOUS_DEPLOY="$(jq -r .previous_deployment "$ST")"
    NEW_DEPLOY="$(jq -r '.new_deployment // empty' "$ST")"
    _gate "restore stage '$STAGE' to deployment $PREVIOUS_DEPLOY and delete only resources recorded in $ST"
    if [ -n "$NEW_DEPLOY" ]; then
      CURRENT_DEPLOY="$(_q apigateway get-stage --rest-api-id "$API" --stage-name "$STAGE" \
        | jq -r '.deploymentId // empty')"
      [ "$CURRENT_DEPLOY" = "$NEW_DEPLOY" ] || {
        echo "FATAL: stage drifted to $CURRENT_DEPLOY after apply; refusing to overwrite a newer deployment" >&2
        exit 2
      }
      _q apigateway update-stage --rest-api-id "$API" --stage-name "$STAGE" \
        --patch-operations "op=replace,path=/deploymentId,value=$PREVIOUS_DEPLOY" >/dev/null
    fi
    while IFS=$'\t' read -r rid path; do
      [ -n "$rid" ] || continue
      _q apigateway delete-resource --rest-api-id "$API" --resource-id "$rid" >/dev/null
      echo "DELETED: $path ($rid)"
    done < <(jq -r '.created_resources | reverse[] | [.id,.path] | @tsv' "$ST")
    _state_update '.rolled_back=true'
    echo "OK: stage restored to deployment $PREVIOUS_DEPLOY; created resources removed"
    ;;
  *)
    _usage
    exit 2
    ;;
esac
