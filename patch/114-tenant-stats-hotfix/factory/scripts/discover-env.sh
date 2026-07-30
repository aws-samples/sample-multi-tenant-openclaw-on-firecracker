#!/usr/bin/env bash
# discover-env.sh - read-only target discovery for a generated claw patch.
#
# Usage: discover-env.sh <region> [manifest.json] [environment.json]
# Output: the requested environment.json plus a confirmation block.
set -euo pipefail

[ $# -ge 1 ] || {
  echo "usage: discover-env.sh <region> [manifest.json] [environment.json]" >&2
  exit 2
}
REGION="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_MANIFEST="$HERE/../manifest.json"
[[ -f "$HERE/../../manifest.json" ]] && DEFAULT_MANIFEST="$HERE/../../manifest.json"
MANIFEST="${2:-$DEFAULT_MANIFEST}"
OUT="${3:-$PWD/environment.json}"

_need() {
  command -v "$1" >/dev/null || {
    echo "FATAL: need '$1'" >&2
    exit 1
  }
}
_need aws
_need jq
Q() { aws "$@" --region "$REGION" --output json; }
warn() { echo "  [!] $*" >&2; }

# Prove a custom domain's URL actually routes to the configured API. Resolve the
# domain's base-path mappings and pick the longest basePath that prefixes this
# URL's base path (the same longest-match API Gateway itself uses), then require
# that mapping's restApiId to equal the expected id. Prints the mapped stage on
# success; a non-zero exit means no mapping backs this API (keep it unresolved).
resolve_custom_domain_stage() {
  local domain="$1" base_path="$2" expected_api="$3" mappings
  mappings="$(Q apigateway get-base-path-mappings --domain-name "$domain" 2>/dev/null)" ||
    return 1
  printf '%s' "$mappings" | jq -e -r \
    --arg path "$base_path" --arg api "$expected_api" '
      (.items // [])
      | map(.bp = (if .basePath == "(none)" then "" else .basePath end))
      | map(select(.bp == "" or $path == .bp or ($path | startswith(.bp + "/"))))
      | sort_by(.bp | length)
      | last
      | select(. != null and .restApiId == $api)
      | .stage
    ' 2>/dev/null
}

echo "== discover-env (READ-ONLY) region=$REGION ==" >&2

ACCT="$(aws sts get-caller-identity --query Account --output text)"
CALLER="$(aws sts get-caller-identity --query Arn --output text)"

# Report every REST API and the facts needed to identify the real control plane.
# Route shape alone is not authoritative: deployments may use explicit resources
# or /{proxy+}, and auth may be API key, IAM, authorizer, or a VPCE resource policy.
API_CANDIDATES="$(Q apigateway get-rest-apis | jq -c '[.items[] | {id, name}]')"
API_FACTS="[]"
for id in $(printf '%s' "$API_CANDIDATES" | jq -r '.[].id'); do
  name="$(printf '%s' "$API_CANDIDATES" |
    jq -r --arg id "$id" 'first(.[] | select(.id == $id) | .name) // "?"')"
  resources="$(Q apigateway get-resources --rest-api-id "$id" 2>/dev/null ||
    printf '%s' '{"items":[]}')"
  paths="$(printf '%s' "$resources" | jq -c '[.items[].path]')"
  has_tenants="$(printf '%s' "$paths" |
    jq 'any(.[]; . == "/tenants" or startswith("/tenants"))')"
  has_hosts="$(printf '%s' "$paths" |
    jq 'any(.[]; . == "/hosts" or startswith("/hosts"))')"
  has_proxy="$(printf '%s' "$paths" | jq 'any(.[]; contains("{proxy+}"))')"

  IFS=$'\t' read -r resource_id method < <(
    printf '%s' "$resources" | jq -r '
      [
        .items[] | select(.resourceMethods) | . as $resource
        | (.resourceMethods | keys[] | select(. != "OPTIONS")) as $method
        | [$resource.id, $method]
      ] | first // ["", ""] | @tsv'
  )
  auth="unknown"
  api_key="unknown"
  if [ -n "$resource_id" ] && [ -n "$method" ]; then
    method_facts="$(Q apigateway get-method --rest-api-id "$id" \
      --resource-id "$resource_id" --http-method "$method" 2>/dev/null |
      jq -c '{auth:.authorizationType, api_key:.apiKeyRequired}')" ||
      method_facts=""
    if [ -n "$method_facts" ]; then
      auth="$(printf '%s' "$method_facts" | jq -r '.auth // "unknown"')"
      api_key="$(printf '%s' "$method_facts" | jq -r '.api_key // "unknown"')"
    fi
  fi
  resource_policy="$(Q apigateway get-rest-api --rest-api-id "$id" 2>/dev/null |
    jq -r 'if (.policy // "") != "" then "yes" else "no" end')" ||
    resource_policy="unknown"

  API_FACTS="$(printf '%s' "$API_FACTS" | jq -c \
    --arg id "$id" --arg name "$name" \
    --argjson tenants "$has_tenants" --argjson hosts "$has_hosts" \
    --argjson proxy "$has_proxy" --arg auth "$auth" \
    --arg api_key "$api_key" --arg policy "$resource_policy" \
    '. + [{
      id:$id, name:$name, has_tenants:$tenants, has_hosts:$hosts,
      proxy:$proxy, method_auth:$auth, api_key_required:$api_key,
      resource_policy:$policy
    }]')"
done

API_PLAUSIBLE="$(printf '%s' "$API_FACTS" |
  jq -c '[.[] | select((.has_tenants and .has_hosts) or .proxy)]')"
API_PLAUSIBLE_N="$(printf '%s' "$API_PLAUSIBLE" | jq 'length')"
API_MATCH="null"
API_CONFIRMED=false
STAGE_NAME=""
API_PROBE_RESULTS="[]"
API_WHY="unresolved: set OC_CONTROL_PLANE_URL and run from the real client call site with its auth headers"

# A route/name match is only a candidate. Resolve an API only when the configured
# client URL identifies it and authenticated calls to every required route succeed.
if [ -n "${OC_CONTROL_PLANE_URL:-}" ]; then
  _need curl
  configured_url="${OC_CONTROL_PLANE_URL%/}"
  configured_host="${configured_url#*://}"
  configured_host="${configured_host%%/*}"
  # Base path as API Gateway stores it: no scheme, no host, no leading slash.
  # "https://api.example.com/prod" -> "prod"; a bare host -> "".
  configured_base_path="${configured_url#*://}"
  configured_base_path="${configured_base_path#"$configured_host"}"
  configured_base_path="${configured_base_path#/}"
  derived_api_id=""
  case "$configured_host" in
    *.execute-api.*.amazonaws.com|*.execute-api.*.amazonaws.com.cn)
      derived_api_id="${configured_host%%.*}"
      ;;
  esac
  configured_api_id="${OC_CONTROL_PLANE_API_ID:-$derived_api_id}"
  if [ -z "$configured_api_id" ]; then
    API_WHY="unresolved: custom domain requires OC_CONTROL_PLANE_API_ID"
  elif [ -n "$derived_api_id" ] && [ "$configured_api_id" != "$derived_api_id" ]; then
    API_WHY="unresolved: configured URL API id disagrees with OC_CONTROL_PLANE_API_ID"
  elif ! printf '%s' "$API_CANDIDATES" |
    jq -e --arg id "$configured_api_id" 'any(.[]; .id == $id)' >/dev/null; then
    API_WHY="unresolved: configured API id is not present in this account/region"
  elif [ -z "$derived_api_id" ] &&
    ! mapped_stage="$(resolve_custom_domain_stage \
      "$configured_host" "$configured_base_path" "$configured_api_id")"; then
    # Custom domain: a passing probe only proves *some* API answers this URL.
    # Require a base-path mapping that actually routes this URL to the configured
    # id, or the probe could confirm an unrelated API and we'd patch the wrong one.
    API_WHY="unresolved: no base-path mapping on $configured_host routes '/$configured_base_path' to API $configured_api_id"
  else
    probe_headers=()
    if [ -n "${OC_CONTROL_PLANE_PROBE_HEADERS_FILE:-}" ]; then
      [ -f "$OC_CONTROL_PLANE_PROBE_HEADERS_FILE" ] || {
        echo "FATAL: OC_CONTROL_PLANE_PROBE_HEADERS_FILE not found" >&2
        exit 2
      }
      jq -e '
        type == "object"
        and all(to_entries[];
          (.key | test("^[A-Za-z0-9-]+$"))
          and (.value | type) == "string"
          and (.value | test("[\r\n\t]") | not)
        )
      ' "$OC_CONTROL_PLANE_PROBE_HEADERS_FILE" >/dev/null || {
        echo "FATAL: probe headers must be a safe string-valued JSON object" >&2
        exit 2
      }
      while IFS=$'\t' read -r header_name header_value; do
        probe_headers+=(-H "$header_name: $header_value")
      done < <(jq -r 'to_entries[] | [.key,.value] | @tsv' \
        "$OC_CONTROL_PLANE_PROBE_HEADERS_FILE")
    fi
    probes_ok=true
    probe_header_count="${#probe_headers[@]}"
    IFS=',' read -r -a probe_paths <<< "${OC_CONTROL_PLANE_PROBE_PATHS:-/tenants,/hosts}"
    for probe_path in "${probe_paths[@]}"; do
      case "$probe_path" in
        /*) ;;
        *)
          echo "FATAL: every OC_CONTROL_PLANE_PROBE_PATHS entry must start with /" >&2
          exit 2
          ;;
      esac
      if [ "$probe_header_count" -gt 0 ]; then
        probe_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
          "${probe_headers[@]}" "${configured_url}${probe_path}" 2>/dev/null || printf '000')"
      else
        probe_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
          "${configured_url}${probe_path}" 2>/dev/null || printf '000')"
      fi
      API_PROBE_RESULTS="$(printf '%s' "$API_PROBE_RESULTS" |
        jq -c --arg path "$probe_path" --arg status "$probe_status" \
          '. + [{path:$path,status:($status | tonumber? // 0)}]')"
      case "$probe_status" in
        2??) ;;
        *) probes_ok=false ;;
      esac
    done
    if [ "$probes_ok" = true ]; then
      if [ -n "${mapped_stage:-}" ]; then
        candidate_stage="$mapped_stage"
      else
        candidate_stage="${configured_base_path%%/*}"
      fi
      if [ -z "$candidate_stage" ]; then
        API_WHY="unresolved: configured client URL does not identify a REST API stage"
      elif ! Q apigateway get-stage --rest-api-id "$configured_api_id" \
        --stage-name "$candidate_stage" >/dev/null 2>&1; then
        API_WHY="unresolved: configured client URL names stage '$candidate_stage', but get-stage cannot confirm it"
      else
        API_MATCH="$configured_api_id"
        STAGE_NAME="$candidate_stage"
        API_CONFIRMED=true
        API_WHY="machine-confirmed by configured client URL, deployed stage, and authenticated required-route probes"
        if [ -n "${mapped_stage:-}" ]; then
          API_WHY="$API_WHY; custom-domain base-path mapping routes to stage $mapped_stage"
        fi
      fi
    else
      API_WHY="unresolved: one or more configured-client authenticated probes failed"
    fi
  fi
fi
if [ "$API_CONFIRMED" != true ]; then
  warn "control-plane API unresolved; no route/name suggestion is safe. Candidate facts:"
  printf '%s' "$API_FACTS" | jq -r '
    .[] | "      - \(.id) (\(.name)) tenants=\(.has_tenants) hosts=\(.has_hosts) " +
    "proxy=\(.proxy) auth=\(.method_auth) api_key=\(.api_key_required) policy=\(.resource_policy)"' >&2
fi

API_DEPLOYED_STAGES="[]"
API_INTEGRATION_TARGET=""
if [ "$API_MATCH" != "null" ]; then
  API_DEPLOYED_STAGES="$(Q apigateway get-stages --rest-api-id "$API_MATCH" |
    jq -c '[.item[]? | {stage:.stageName, deployed:.lastUpdatedDate}]')"
  matched_resources="$(Q apigateway get-resources --rest-api-id "$API_MATCH")"
  # Derive the control-plane Lambda ONLY from the integrations behind the probed,
  # authenticated paths, and require them to resolve to a single function. Taking
  # the first method on any resource could bind a co-hosted Lambda on the same
  # API (a different backend) and point the patch at the wrong HOSTS_TABLE/ASG.
  # An exact-path API is required here; a probed path with no matching resource
  # (e.g. a {proxy+}-only API) leaves the target empty so OC_API_FUNCTION_NAME
  # must resolve it rather than the script guessing.
  probe_paths_json="$(printf '%s\n' "${probe_paths[@]}" | jq -R . | jq -cs .)"
  lambda_targets=()
  resolved_paths=()
  while IFS=$'\t' read -r probed_path probed_rid probed_method; do
    [ -n "$probed_path" ] && [ -n "$probed_rid" ] && [ -n "$probed_method" ] || continue
    probed_target="$(
      Q apigateway get-integration --rest-api-id "$API_MATCH" \
        --resource-id "$probed_rid" --http-method "$probed_method" 2>/dev/null |
        jq -r '
          (.uri // "") as $uri
          | (try ($uri | capture("functions/(?<arn>arn:[^/]+)/invocations").arn)
             catch "")'
    )" || probed_target=""
    [ -n "$probed_target" ] || continue
    # ${arr[*]:-} guards the empty-array expansion under set -u (bash 3.2).
    case " ${resolved_paths[*]:-} " in
      *" $probed_path "*) ;;
      *) resolved_paths+=("$probed_path") ;;
    esac
    case " ${lambda_targets[*]:-} " in
      *" $probed_target "*) ;;
      *) lambda_targets+=("$probed_target") ;;
    esac
  done < <(
    printf '%s' "$matched_resources" | jq -r --argjson probes "$probe_paths_json" '
      [
        .items[]
        | select(.resourceMethods and (.path as $p | $probes | index($p)))
        | . as $resource
        # The probes are GET reads; resolve the method that actually serves them —
        # GET, or the ANY catch-all as fallback. Never a sibling POST/PUT, whose
        # write-handler may be a different Lambda (wrong fleet / false multi-target).
        | (.resourceMethods
           | if has("GET") then "GET" elif has("ANY") then "ANY" else empty end) as $method
        | [$resource.path, $resource.id, $method]
      ] | .[] | @tsv'
  )
  # EVERY probed path must resolve to an exact-path Lambda integration. A hybrid API
  # where one probed path is an exact resource and another only matches {proxy+}
  # leaves the proxy path unresolved above; adopting the single exact-path target
  # would bind the patch to a Lambda that does not serve the other path (wrong
  # HOSTS_TABLE/fleet). Fail closed to explicit config rather than guess.
  if [ "${#resolved_paths[@]}" -lt "${#probe_paths[@]}" ]; then
    API_INTEGRATION_TARGET=""
    warn "only ${#resolved_paths[@]}/${#probe_paths[@]} probed control-plane paths resolve to an exact-path Lambda integration; set OC_API_FUNCTION_NAME to bind the control plane explicitly."
  else
    case "${#lambda_targets[@]}" in
      0) API_INTEGRATION_TARGET="" ;;
      1) API_INTEGRATION_TARGET="${lambda_targets[0]}" ;;
      *)
        echo "FATAL: probed control-plane paths resolve to multiple Lambda targets:" >&2
        printf '       %s\n' "${lambda_targets[@]}" >&2
        echo "       refusing to guess which is the control plane; set OC_API_FUNCTION_NAME." >&2
        exit 2
        ;;
    esac
  fi
fi

API_FUNCTION=""
API_QUALIFIER=""
if [ "$API_CONFIRMED" = true ] && [ -n "$API_INTEGRATION_TARGET" ]; then
  function_ref="${API_INTEGRATION_TARGET#*:function:}"
  if [ "$function_ref" != "$API_INTEGRATION_TARGET" ]; then
    API_FUNCTION="${function_ref%%:*}"
    if [ "$function_ref" != "$API_FUNCTION" ]; then
      API_QUALIFIER="${function_ref#*:}"
    fi
  fi
fi
if [ -n "${OC_API_FUNCTION_NAME:-}" ]; then
  API_FUNCTION="$OC_API_FUNCTION_NAME"
  API_QUALIFIER="${OC_API_FUNCTION_QUALIFIER:-}"
fi

API_ALIASES="[]"
API_LATEST_SHA=""
DISPATCH_ESM_TARGET=""
API_FUNCTION_CONFIG="{}"
if [ -n "$API_FUNCTION" ]; then
  API_ALIASES="$(Q lambda list-aliases --function-name "$API_FUNCTION" 2>/dev/null |
    jq -c '[.Aliases[] | {alias:.Name, version:.FunctionVersion}]')" ||
    API_ALIASES="[]"
  # shellcheck disable=SC2016 # AWS qualifier is the literal name "$LATEST".
  API_LATEST_SHA="$(Q lambda get-function-configuration --function-name "$API_FUNCTION" \
    --qualifier '$LATEST' 2>/dev/null | jq -r '.CodeSha256 // ""')" ||
    API_LATEST_SHA=""
  DISPATCH_ESM_TARGET="$(Q lambda list-event-source-mappings \
    --function-name "$API_FUNCTION" 2>/dev/null | jq -r '
      first(
        .EventSourceMappings[]
        | select(.EventSourceArn | test("sqs"; "i"))
        | (.FunctionArn | split(":")) as $parts
        | if ($parts | length) > 7 then $parts[-1] else "$LATEST" end
      ) // ""')" || DISPATCH_ESM_TARGET=""
  if [ -n "$API_QUALIFIER" ]; then
    API_FUNCTION_CONFIG="$(Q lambda get-function-configuration \
      --function-name "$API_FUNCTION" --qualifier "$API_QUALIFIER" 2>/dev/null ||
      printf '%s' '{}')"
  else
    API_FUNCTION_CONFIG="$(Q lambda get-function-configuration \
      --function-name "$API_FUNCTION" 2>/dev/null || printf '%s' '{}')"
  fi
else
  warn "serving Lambda unresolved; API must be confirmed before ASG correlation"
fi

# Resolve the hosts table from the serving Lambda contract, then use its live
# instance ids as the machine identity for the Firecracker fleet.
HOSTS_TABLE="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '.Environment.Variables.HOSTS_TABLE // empty')"
HOST_IDS="[]"
if [ -n "$HOSTS_TABLE" ]; then
  HOST_IDS="$(Q dynamodb scan --table-name "$HOSTS_TABLE" \
    --projection-expression instance_id | jq -c '[.Items[].instance_id.S] | unique | sort')"
else
  warn "HOSTS_TABLE is absent from the confirmed serving Lambda; host ASG will remain unresolved"
fi

# Select only the ASG whose live instance set exactly matches the hosts-table
# control-plane ledger. Names are diagnostic only and cannot confirm identity.
ASG_ALL="$(Q autoscaling describe-auto-scaling-groups | jq -c '
  [.AutoScalingGroups[] | {
    name:.AutoScalingGroupName,
    type:(if .MixedInstancesPolicy then "mip"
          elif .LaunchTemplate then "plain" else "none" end),
    lt:(.MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification
        // .LaunchTemplate // {}),
    instances:[.Instances[]?.InstanceId] | sort
  }]')"
if [ -n "${OC_HOST_ASG_NAME:-}" ]; then
  ASG_NAMED_CANDIDATES="$(printf '%s' "$ASG_ALL" |
    jq -c --arg name "$OC_HOST_ASG_NAME" '[.[] | select(.name == $name)]')"
  ASG_NAME_FILTER_WHY="explicit OC_HOST_ASG_NAME"
else
  ASG_NAMED_CANDIDATES="$(printf '%s' "$ASG_ALL" | jq -c '
    [.[] | . as $candidate | (.lt.LaunchTemplateName // "") as $lt_name
      | select(
          (($candidate.name | test("host"; "i")) or ($lt_name | test("host"; "i")))
          and (($candidate.name | test("edge"; "i")) | not)
          and (($lt_name | test("edge"; "i")) | not)
        )
    ]')"
  ASG_NAME_FILTER_WHY="host-not-edge names (diagnostic prefilter only)"
fi
ASG_CANDIDATES="$(printf '%s' "$ASG_NAMED_CANDIDATES" |
  jq -c --argjson hosts "$HOST_IDS" '
    [.[] | select(
      ($hosts | length) > 0
      and ((.instances | sort) == ($hosts | sort))
    )]')"

ASG_CANDIDATE_N="$(printf '%s' "$ASG_CANDIDATES" | jq 'length')"
ASG_NAME=""
ASG_LT_ID=""
ASG_LT_NAME=""
ASG_LT_VER=""
ASG_TYPE="none"
if [ "$ASG_CANDIDATE_N" -eq 1 ]; then
  ASG_NAME="$(printf '%s' "$ASG_CANDIDATES" | jq -r '.[0].name')"
  ASG_LT_ID="$(printf '%s' "$ASG_CANDIDATES" |
    jq -r '.[0].lt.LaunchTemplateId // ""')"
  ASG_LT_VER="$(printf '%s' "$ASG_CANDIDATES" | jq -r '.[0].lt.Version // ""')"
  ASG_TYPE="$(printf '%s' "$ASG_CANDIDATES" | jq -r '.[0].type')"
  ASG_WHY="machine-confirmed: ASG instance set exactly matches $HOSTS_TABLE ($ASG_NAME_FILTER_WHY)"
  ASG_LT_NAME="$(Q ec2 describe-launch-templates --launch-template-ids "$ASG_LT_ID" \
    2>/dev/null | jq -r '.LaunchTemplates[0].LaunchTemplateName // ""')" ||
    ASG_LT_NAME=""
  if printf '%s' "$ASG_LT_NAME" | grep -qi edge; then
    warn "selected ASG '$ASG_NAME' points at edge LT '$ASG_LT_NAME'; target is unresolved"
    ASG_NAME=""
    ASG_LT_ID=""
    ASG_LT_VER=""
    ASG_TYPE="none"
    ASG_CANDIDATE_N=0
    ASG_WHY="rejected because selected LT looked like edge"
  fi
else
  ASG_WHY="unresolved: $ASG_CANDIDATE_N candidates correlate exactly with hosts table ($ASG_NAME_FILTER_WHY)"
  warn "host ASG unresolved. Do not run apply-lt.sh. ASG/ledger facts:"
  printf '%s' "$ASG_ALL" | jq -r '
    .[] | "      - \(.name) type=\(.type) LT=" +
    "\(.lt.LaunchTemplateName // .lt.LaunchTemplateId // "?") v\(.lt.Version // "?") " +
    "instances=\(.instances | join(","))"' >&2
fi
# shellcheck disable=SC2016 # These are literal EC2 launch-template version aliases.
case "$ASG_LT_VER" in
  '$Latest'|'$Default')
    warn "ASG pins floating version '$ASG_LT_VER'; pin a numeric version before LT rollout"
    ;;
esac

DISPATCH_MODE="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '.Environment.Variables.DISPATCH_MODE // "unknown"')"
LOGGING_ON="$(
  Q dynamodb list-tables | jq -r '
    if any(.TableNames[]; test("observability|opensearch"))
    then "likely-true" else "unknown" end'
)" || LOGGING_ON="unknown"
FIX_VERDICTS="$(jq -c --arg dispatch "$DISPATCH_MODE" --arg logging "$LOGGING_ON" '
  [.fixes[] | {
    id, applies_when,
    verdict: (
      if .applies_when == "always"
      then "IN-SCOPE (always)"
      elif (.applies_when | test("dispatch.mode==ddb"))
      then (if $dispatch == "ddb" then "IN-SCOPE"
            else "CHECK (dispatch.mode=" + $dispatch + ")" end)
      elif (.applies_when | test("logging.enabled==true"))
      then "CHECK (logging=" + $logging + ")"
      else "UNRESOLVED (unsupported applies_when predicate)"
      end
    )
  }]' "$MANIFEST")"

# The apply driver needs the assets bucket + FB_* render bindings. They live as real
# (already-rendered) values in the ASG-pinned LT's UserData. Decode it and parse them out; leave any
# we can't resolve empty so the CONFIRM block flags them for the operator instead of a silent fail.
FB_BINDINGS='{}'
ASSETS_BUCKET=""
if [ -n "${ASG_LT_ID:-}" ] && [ -n "${ASG_LT_VER:-}" ]; then
  LT_UD="$(Q ec2 describe-launch-template-versions --launch-template-id "$ASG_LT_ID" \
    --versions "$ASG_LT_VER" --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
    --output text 2>/dev/null | base64 -d 2>/dev/null | { gunzip 2>/dev/null || cat; } || true)"
  if [ -n "$LT_UD" ]; then
    _grab() { printf '%s' "$LT_UD" | grep -oE "$1=[^ \"']+" | head -1 | cut -d= -f2- ; }
    ASSETS_BUCKET="$(_grab ASSETS_BUCKET)"
    FB_BINDINGS="$(jq -n \
      --arg r "$(_grab FB_REGION)" --arg h "$(_grab FB_STREAM_HOST)" \
      --arg v "$(_grab FB_STREAM_VM)" --arg b "$ASSETS_BUCKET" \
      '{FB_REGION:$r, FB_STREAM_HOST:$h, FB_STREAM_VM:$v, ASSETS_BUCKET:$b}
       | with_entries(select(.value != ""))')"
  fi
fi

jq -n \
  --arg region "$REGION" --arg account "$ACCT" --arg caller "$CALLER" \
  --arg api "$API_MATCH" --arg api_stage "$STAGE_NAME" --arg api_why "$API_WHY" \
  --argjson api_confirmed "$API_CONFIRMED" --argjson api_probes "$API_PROBE_RESULTS" \
  --argjson api_candidates "$API_CANDIDATES" --argjson api_facts "$API_FACTS" \
  --argjson stages "$API_DEPLOYED_STAGES" --arg api_target "$API_INTEGRATION_TARGET" \
  --argjson aliases "$API_ALIASES" --arg latest_sha "$API_LATEST_SHA" \
  --arg esm "$DISPATCH_ESM_TARGET" --arg asg "$ASG_NAME" \
  --arg lt_id "$ASG_LT_ID" --arg lt_name "$ASG_LT_NAME" \
  --arg lt_version "$ASG_LT_VER" --arg asg_type "$ASG_TYPE" \
  --arg asg_candidates "$ASG_CANDIDATE_N" --arg api_candidates_n "$API_PLAUSIBLE_N" \
  --arg asg_why "$ASG_WHY" --arg hosts_table "$HOSTS_TABLE" \
  --argjson hosts "$HOST_IDS" --argjson fixes "$FIX_VERDICTS" \
  --arg dispatch "$DISPATCH_MODE" --arg api_function "$API_FUNCTION" \
  --arg api_qualifier "$API_QUALIFIER" \
  --argjson bindings "$FB_BINDINGS" --arg assets_bucket "$ASSETS_BUCKET" '
  {
    region:$region, account:$account, caller_arn:$caller,
    assets_bucket:(if $assets_bucket == "" then null else $assets_bucket end),
    bindings:$bindings,
    control_plane_api:{
      id:(if $api == "null" then null else $api end),
      stage:(if $api_stage == "" then null else $api_stage end),
      why:$api_why, candidates:$api_candidates,
      candidate_facts:$api_facts, candidate_count:($api_candidates_n | tonumber),
      deployed_stages:$stages, lambda_integration:$api_target,
      confirmed:$api_confirmed, probe_results:$api_probes,
      note:"Resolved only by configured client URL plus successful authenticated required-route probes."
    },
    lambda_link:{
      function:$api_function, api_invokes:$api_target, aliases:$aliases,
      serving_qualifier:$api_qualifier, latest_code_sha256:$latest_sha,
      dispatch_sqs_esm_binds:$esm
    },
    asg:{
      name:$asg, why:$asg_why, lt_id:$lt_id, lt_name:$lt_name,
      lt_version_pinned:$lt_version, type:$asg_type,
      candidate_count:($asg_candidates | tonumber),
      confirmed:($asg != ""),
      note:"Resolved only when the ASG live instance set exactly equals the HOSTS_TABLE ledger."
    },
    hosts:{table:$hosts_table, instance_ids:$hosts, count:($hosts | length)},
    dispatch_mode:$dispatch, fix_applicability:$fixes
  }' > "$OUT"

{
  echo
  echo "================ CONFIRM BEFORE APPLY ================"
  echo "account/region : $ACCT / $REGION"
  echo "caller         : $CALLER"
  echo "API confirmed  : $API_MATCH ($API_WHY)"
  echo "API stage      : ${STAGE_NAME:-<unresolved>}"
  printf '%s' "$API_FACTS" | jq -r '
    .[] | "  API \(.id) \(.name): tenants=\(.has_tenants) hosts=\(.has_hosts) " +
    "proxy=\(.proxy) auth=\(.method_auth) key=\(.api_key_required) policy=\(.resource_policy)"'
  echo "API integration: ${API_INTEGRATION_TARGET:-<unresolved>}"
  echo "Lambda aliases : $(printf '%s' "$API_ALIASES" | jq -c '.')"
  echo "dispatch ESM   : ${DISPATCH_ESM_TARGET:-<unresolved>}"
  echo "HOST ASG       : ${ASG_NAME:-<unresolved>} type=$ASG_TYPE LT=${ASG_LT_NAME:-$ASG_LT_ID} v=$ASG_LT_VER candidates=$ASG_CANDIDATE_N"
  echo "hosts          : $(printf '%s' "$HOST_IDS" | jq 'length') ($HOSTS_TABLE)"
  echo "fix applicability:"
  printf '%s' "$FIX_VERDICTS" |
    jq -r '.[] | "  \(.id): \(.verdict) [\(.applies_when)]"'
  echo
  echo "[ ] API: confirmed must be true; otherwise do not run route apply."
  echo "[ ] ASG: confirmed must be true; otherwise do not run LT apply."
  echo "written: $OUT (discovery made no AWS changes)"
  echo "======================================================"
} >&2

echo "$OUT"
