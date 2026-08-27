#!/usr/bin/env bash
# discover-env.sh - read-only target discovery for a generated claw patch.
#
# Usage: discover-env.sh <region> [manifest.json]
# Output: environment.json next to the patch manifest plus a confirmation block.
set -euo pipefail

[ $# -ge 1 ] || {
  echo "usage: discover-env.sh <region> [manifest.json]" >&2
  exit 2
}
REGION="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${2:-$HERE/../manifest.json}"
OUT="$HERE/../environment.json"

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
      API_MATCH="$configured_api_id"
      API_CONFIRMED=true
      API_WHY="machine-confirmed by configured client URL and authenticated required-route probes"
      if [ -n "${mapped_stage:-}" ]; then
        API_WHY="$API_WHY; custom-domain base-path mapping routes to stage $mapped_stage"
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

# Since #389 the LT no longer carries init-host.sh; it carries a bootstrap that downloads
# an immutable, digest-named object from the assets bucket. Patching that path needs the
# bucket coordinate, which nothing here used to discover — so the LT-form patch had no
# target at all. Resolve it the same way as HOSTS_TABLE (from the confirmed serving
# Lambda), then require the LT's own bootstrap to name the same bucket. One source is a
# guess; two independent sources agreeing is a confirmation, and the LT is the only source
# that proves which bucket the hosts will actually read at boot.
ASSETS_BUCKET="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '.Environment.Variables.ASSETS_BUCKET // empty')"
BOOTSTRAP_INFO="{}"
ASSETS_CONFIRMED=false
ASSETS_WHY="unresolved"
LTU="$HERE/lt-userdata.py"
if [ -z "$ASSETS_BUCKET" ]; then
  ASSETS_WHY="ASSETS_BUCKET absent from the confirmed serving Lambda"
  warn "$ASSETS_WHY; do not run an s3-bootstrap LT patch"
elif [ -z "$ASG_LT_ID" ]; then
  ASSETS_WHY="LT unresolved, so the bucket the hosts read cannot be corroborated"
  warn "$ASSETS_WHY"
elif [ ! -f "$LTU" ]; then
  ASSETS_WHY="lt-userdata.py missing next to discover-env.sh; cannot classify LT user-data"
  warn "$ASSETS_WHY"
else
  LT_UD_FILE="$(mktemp)"
  # shellcheck disable=SC2064 # expand the path now so the trap survives $LT_UD_FILE reuse.
  trap "rm -f '$LT_UD_FILE'" EXIT
  if Q ec2 describe-launch-template-versions --launch-template-id "$ASG_LT_ID" \
       --versions "$ASG_LT_VER" |
       jq -r '.LaunchTemplateVersions[0].LaunchTemplateData.UserData // ""' |
       base64 -d > "$LT_UD_FILE" 2>/dev/null &&
     BOOTSTRAP_INFO="$(python3 "$LTU" inspect "$LT_UD_FILE" 2>/dev/null)"; then
    LT_FORM="$(printf '%s' "$BOOTSTRAP_INFO" | jq -r '.form // "unknown"')"
    LT_BUCKET="$(printf '%s' "$BOOTSTRAP_INFO" | jq -r '.bucket // ""')"
    if [ "$LT_FORM" = "gzip-inline" ]; then
      # Pre-#389: the script is inside user-data, so no object is bound and there is
      # nothing to corroborate. Not an error — the bucket is simply not part of that path.
      ASSETS_WHY="LT is gzip-inline (pre-#389); no bootstrap object is bound"
    elif [ "$LT_BUCKET" = "$ASSETS_BUCKET" ]; then
      ASSETS_CONFIRMED=true
      ASSETS_WHY="machine-confirmed: LT v$ASG_LT_VER bootstrap and the serving Lambda name the same bucket"
    else
      ASSETS_WHY="MISMATCH: LT bootstrap reads '$LT_BUCKET' but the serving Lambda declares '$ASSETS_BUCKET'"
      warn "$ASSETS_WHY; do not patch either until this is explained"
    fi
  else
    BOOTSTRAP_INFO="{}"
    ASSETS_WHY="LT user-data matched no known bootstrap form; refusing to guess"
    warn "$ASSETS_WHY"
  fi
  rm -f "$LT_UD_FILE"
  trap - EXIT
fi

DISPATCH_MODE="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '.Environment.Variables.DISPATCH_MODE // "unknown"')"
LOGGING_ON="$(
  Q dynamodb list-tables | jq -r '
    if any(.TableNames[]; test("observability|opensearch"))
    then "likely-true" else "unknown" end'
)" || LOGGING_ON="unknown"
# #396 — can a role-gated oracle (376's viewer-denied check) run here at all?
# Roles come ONLY from a signature-verified Cognito id_token's cognito:groups
# (deploy/lambda/api/core/auth.py:_role_from_claims); an API Gateway api-key carries no
# role. So "fetch a viewer-scoped key" does not exist by construction — the 2026-07-27
# production report's "this environment only has admin/private keys" really means "this
# account has no Cognito pool, so no viewer principal can exist". Read the three facts
# that decide it here, so the executor neither guesses nor passes a no-Bearer call off
# as a viewer call.
# FIRST: did the serving-Lambda config read actually SUCCEED? `{}` is this script's sentinel
# for "could not read it" (the two `|| printf '{}'` fallbacks above, plus an unresolved
# API_FUNCTION), and a successful get-function-configuration is never an empty object. Without
# this flag an AccessDenied on that read looks exactly like "the deployment declares no
# COGNITO_USER_POOL_ID", i.e. a failed probe reported as a confirmed fact — it would send the
# operator off to enable console_auth when the real problem is their own read permission.
API_CONFIG_READABLE=true
if [ "$(printf '%s' "$API_FUNCTION_CONFIG" | jq -r 'length')" = "0" ]; then
  API_CONFIG_READABLE=false
fi
# Both defaults MIRROR the runtime (core/clients.py): an ABSENT RBAC_ENABLED means
# enabled, and an absent DEFAULT_NO_JWT_ROLE means viewer. Reporting "unset" as disabled
# would hand a healthy deployment a false "RBAC short-circuits" verdict and skip an oracle
# that should have run. Runtime also lowercases before comparing, so do the same here.
RBAC_ON="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '(.Environment.Variables.RBAC_ENABLED // "true") | ascii_downcase')"
NO_JWT_ROLE="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '.Environment.Variables.DEFAULT_NO_JWT_ROLE // "viewer"')"
USER_POOL="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '.Environment.Variables.COGNITO_USER_POOL_ID // ""')"
POOL_CLIENT="$(printf '%s' "$API_FUNCTION_CONFIG" |
  jq -r '.Environment.Variables.COGNITO_CLIENT_ID // ""')"
ROLE_GROUPS='[]'
VIEWER_MEMBERS=null
# A FAILED probe is not a confirmed absence. Collapsing an AccessDenied or a transient AWS
# error into `[]` would report "this pool has no viewer group" and send the operator off to
# create one that already exists — so keep the read's success/failure and branch on it.
GROUPS_READABLE=false
VIEWER_GROUP_PRESENT=false
# Same trap one level down: `viewer_members: null` would otherwise mean BOTH "no pool / no
# viewer group" and "the member read was denied". The cleanup contract's last rule is "confirm
# the viewer group member count returned to its pre-run value", so a caller who cannot read
# members cannot perform its own mandatory post-run check — that must BLOCK before anything is
# created, not surface after a permanent-password principal already exists.
MEMBERS_READABLE=false
DISCRIMINATOR_GROUP=""
if [ -n "$USER_POOL" ]; then
  if GROUPS_JSON="$(Q cognito-idp list-groups --user-pool-id "$USER_POOL" 2>/dev/null)" &&
    ROLE_GROUPS="$(printf '%s' "$GROUPS_JSON" | jq -c '[.Groups[].GroupName] | sort')"; then
    GROUPS_READABLE=true
    if printf '%s' "$ROLE_GROUPS" | jq -e 'index("viewer")' >/dev/null 2>&1; then
      VIEWER_GROUP_PRESENT=true
      if VIEWER_MEMBERS="$(Q cognito-idp list-users-in-group --user-pool-id "$USER_POOL" \
        --group-name viewer 2>/dev/null | jq '.Users | length')" &&
        [ -n "$VIEWER_MEMBERS" ]; then
        MEMBERS_READABLE=true
      else
        VIEWER_MEMBERS=null
      fi
    fi
    # The step-8 discriminator needs a group the invalid-Bearer fallback cannot produce, i.e. one
    # ranked ABOVE viewer (_ROLE_RANK: viewer 0 < operator 1 < admin 2). Without one, the whole
    # oracle is undiscriminating and OBTAINABLE would only be discovered to be wrong AFTER a
    # permanent-password principal exists. Prefer operator: it is the least privilege that still
    # clears an operator-gated route, and admin additionally passes ownership checks.
    if printf '%s' "$ROLE_GROUPS" | jq -e 'index("operator")' >/dev/null 2>&1; then
      DISCRIMINATOR_GROUP="operator"
    elif printf '%s' "$ROLE_GROUPS" | jq -e 'index("admin")' >/dev/null 2>&1; then
      DISCRIMINATOR_GROUP="admin"
    fi
  else
    ROLE_GROUPS='[]'
  fi
fi
# Group presence is NOT credential availability. An existing member is useless to the
# runner (its password is not held), so the only actionable question is "can this caller
# MINT a throwaway member and exchange it for an id_token?" — which needs an app client
# that permits a password auth flow. Read that instead of assuming it.
AUTH_FLOWS='[]'
CLIENT_HAS_SECRET=null
POOL_MFA="unknown"
FIRST_AUTH_FACTORS='[]'
PASSWORD_FIRST_FACTOR="unknown"
# Public, derivable from region + pool id, and carries nothing secret. Emitted so the executor
# verifies the token the way the runtime does instead of merely decoding it.
JWKS_URL=""
if [ -n "$USER_POOL" ]; then
  JWKS_URL="https://cognito-idp.$REGION.amazonaws.com/$USER_POOL/.well-known/jwks.json"
fi
MINT_OK=false
AUTH_MODE="none"
AUTH_IAM="initiate-auth needs no IAM of its own"
# An imported or enterprise pool can REQUIRE MFA. The throwaway user would then get an MFA
# challenge instead of an id_token — and we would only find out after Cognito had already
# been mutated. OPTIONAL is fine: a fresh user with no factor configured is not challenged.
if [ -n "$USER_POOL" ]; then
  if POOL_DESC="$(Q cognito-idp describe-user-pool --user-pool-id "$USER_POOL" 2>/dev/null)"; then
    POOL_MFA="$(printf '%s' "$POOL_DESC" |
      jq -r '.UserPool.MfaConfiguration // "unknown"')" || POOL_MFA="unknown"
    # Choice-based sign-in (2024+) lets a pool allow EMAIL_OTP/SMS_OTP/WEB_AUTHN as FIRST auth
    # factors. Two things follow, both of which this card has already been burned by:
    #   1. AdminCreateUser only generates a temporary password "unless you have passwordless
    #      options active for your user pool" (API reference, verbatim) — so on such a pool the
    #      canary's credential state is NOT what an omitted TemporaryPassword implies. The steps
    #      therefore stop inferring it and supply an explicit random one instead.
    #   2. If PASSWORD is not an allowed first factor, a password exchange cannot be the first
    #      factor at all, however permissive the app client's ExplicitAuthFlows look — and that
    #      would surface only AFTER Cognito had been mutated.
    # An ABSENT SignInPolicy is the legacy shape and means password-based, which is a documented
    # default rather than a guess; a FAILED describe cannot reach here, because it leaves
    # POOL_MFA=unknown and that already lands on BLOCKED.
    FIRST_AUTH_FACTORS="$(printf '%s' "$POOL_DESC" |
      jq -c '.UserPool.Policies.SignInPolicy.AllowedFirstAuthFactors // []')" ||
      FIRST_AUTH_FACTORS='[]'
    if [ "$FIRST_AUTH_FACTORS" = "[]" ]; then
      PASSWORD_FIRST_FACTOR="legacy-default"
    elif printf '%s' "$FIRST_AUTH_FACTORS" | jq -e 'index("PASSWORD")' >/dev/null 2>&1; then
      PASSWORD_FIRST_FACTOR="true"
    else
      PASSWORD_FIRST_FACTOR="false"
    fi
  fi
fi
if [ -n "$USER_POOL" ] && [ -n "$POOL_CLIENT" ]; then
  CLIENT_DESC="$(Q cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL" \
    --client-id "$POOL_CLIENT" 2>/dev/null)" || CLIENT_DESC=""
  if [ -n "$CLIENT_DESC" ]; then
    AUTH_FLOWS="$(printf '%s' "$CLIENT_DESC" |
      jq -c '[.UserPoolClient.ExplicitAuthFlows // []] | flatten | sort')" || AUTH_FLOWS='[]'
    CLIENT_HAS_SECRET="$(printf '%s' "$CLIENT_DESC" |
      jq '(.UserPoolClient.ClientSecret // "") != ""')" || CLIENT_HAS_SECRET=null
    # A client secret is not fatal, but it forces SECRET_HASH on every auth call; treat it
    # as "not a one-liner" and make the executor decide rather than silently promising it.
    #
    # The two password flows need DIFFERENT exchange commands: a client that allows only
    # the admin-only flow cannot be driven with plain `initiate-auth`. Emit the command
    # that actually matches, so the checklist never hands the executor an invalid call.
    # MFA must be EXPLICITLY benign. `unknown` means the describe-user-pool probe failed or
    # returned something unexpected — and a failed probe is not a negative result. Reading it
    # as "not ON, so fine" would let an operator mutate Cognito and only then discover that a
    # required factor turns the exchange into a challenge. (Same mistake as reading an absent
    # RBAC_ENABLED as disabled, and a denied list-groups as an absent group.)
    if [ "$CLIENT_HAS_SECRET" = "false" ] &&
      { [ "$POOL_MFA" = "OFF" ] || [ "$POOL_MFA" = "OPTIONAL" ]; } &&
      [ "$PASSWORD_FIRST_FACTOR" != "false" ]; then
      # Only the FLOW MODE is exposed, never a ready-made exchange command. Such a command would
      # have to carry the username and the password, and a password on a command line is readable
      # by anyone on the box (/proc/<pid>/cmdline). credential_steps says which call to make; the
      # executor supplies the values through --cli-input-json from a mode-0600 file.
      if printf '%s' "$AUTH_FLOWS" |
        jq -e 'any(.[]; . == "ALLOW_USER_PASSWORD_AUTH")' >/dev/null 2>&1; then
        MINT_OK=true
        AUTH_MODE="plain"
      elif printf '%s' "$AUTH_FLOWS" |
        jq -e 'any(.[]; . == "ALLOW_ADMIN_USER_PASSWORD_AUTH")' >/dev/null 2>&1; then
        MINT_OK=true
        AUTH_MODE="admin-only"
        AUTH_IAM="this client permits ONLY the admin-only flow, so the caller additionally needs cognito-idp:AdminInitiateAuth"
      fi
    fi
  fi
fi
# VACUOUS / BLOCKED must downgrade a role oracle to MANUAL_CLI_REVIEW — never PASS.
# The readability checks come FIRST: every fact below is read out of the serving Lambda's
# environment, so if that read failed there is nothing to conclude — not even VACUOUS.
if [ "$API_CONFIG_READABLE" != "true" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="could not read the serving Lambda's configuration (unresolved function, AccessDenied, or a transient AWS error), so RBAC_ENABLED / COGNITO_USER_POOL_ID are UNKNOWN, not absent. This is a FAILED PROBE: fix the read access and re-run discovery, or record MANUAL_CLI_REVIEW. Do NOT read this as 'console_auth.enabled is false'"
  warn "$ROLE_WHY"
elif [ "$RBAC_ON" != "true" ]; then
  ROLE_VERDICT="VACUOUS"
  ROLE_WHY="RBAC_ENABLED=$RBAC_ON on the serving qualifier: _rbac_check short-circuits every route, so a role oracle proves nothing"
  warn "$ROLE_WHY"
elif [ -z "$USER_POOL" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="the serving Lambda declares no COGNITO_USER_POOL_ID (console_auth.enabled is false), so no role-bearing principal can exist in this account"
  warn "$ROLE_WHY"
elif [ "$GROUPS_READABLE" != "true" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="could not inspect the user pool's groups (AccessDenied or a transient AWS error). This is a FAILED PROBE, not a confirmed absence — fix the read access and re-run discovery, or record MANUAL_CLI_REVIEW. Do not create anything on the assumption the group is missing"
  warn "$ROLE_WHY"
elif [ "$VIEWER_GROUP_PRESENT" != "true" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="the user pool was read successfully and has no 'viewer' group (groups=$ROLE_GROUPS)"
  warn "$ROLE_WHY"
elif [ -z "$DISCRIMINATOR_GROUP" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="a 'viewer' group exists but this pool has NEITHER 'operator' NOR 'admin' (groups=$ROLE_GROUPS), so the credential_steps step-8 discriminator cannot run. Without a group ranked above viewer there is nothing the invalid-Bearer fallback cannot also produce, which means a viewer 403 would prove nothing — and you would only find that out AFTER creating a permanent-password principal. Record MANUAL_CLI_REVIEW"
  warn "$ROLE_WHY"
elif [ "$NO_JWT_ROLE" != "viewer" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="DEFAULT_NO_JWT_ROLE=$NO_JWT_ROLE on the serving qualifier, which OUTRANKS viewer — so a call with NO Authorization header already passes an operator-gated write (and sets is_admin=true). That destroys the step-8 discriminator: 'the write is no longer 403' becomes satisfiable with NO TOKEN AT ALL, so it stops proving that anything was verified, and the viewer half it was supposed to underwrite becomes a false green. The oracle can still be EXECUTED here, but its result is not self-verifying, so record MANUAL_CLI_REVIEW. The only sound alternative is to pick a route gated ABOVE $NO_JWT_ROLE, which is patch-specific and outside discovery's knowledge"
  warn "$ROLE_WHY"
elif [ "$MEMBERS_READABLE" != "true" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="the 'viewer' group exists but its membership could not be listed (AccessDenied or a transient AWS error). This is a FAILED PROBE, not 'the group is empty' — and it also means the cleanup contract's post-run member-count re-check cannot be performed, so a minted principal could be left behind unnoticed. Grant cognito-idp:ListUsersInGroup and re-run discovery, or record MANUAL_CLI_REVIEW"
  warn "$ROLE_WHY"
elif [ "$MINT_OK" != "true" ]; then
  ROLE_VERDICT="BLOCKED"
  ROLE_WHY="a 'viewer' group exists but this caller has no one-step way to obtain a viewer id_token: app client='$POOL_CLIENT' flows=$AUTH_FLOWS has_secret=$CLIENT_HAS_SECRET mfa=$POOL_MFA first_auth_factors=$FIRST_AUTH_FACTORS password_first_factor=$PASSWORD_FIRST_FACTOR (need ALLOW_USER_PASSWORD_AUTH or ALLOW_ADMIN_USER_PASSWORD_AUTH, no client secret, MfaConfiguration not ON — a required MFA factor turns the exchange into a challenge instead of an id_token — and PASSWORD among the pool's allowed FIRST auth factors: a choice-based/passwordless pool can refuse a password exchange no matter how permissive ExplicitAuthFlows looks, and you would find that out only after Cognito had been mutated). Existing group members do not count — their passwords are not held"
  warn "$ROLE_WHY"
else
  ROLE_VERDICT="OBTAINABLE"
  ROLE_WHY="'viewer' group exists and app client '$POOL_CLIENT' permits a password auth flow; mint a throwaway member, run the oracle, then delete it. OBTAINABLE means the READ-VISIBLE preconditions hold — it does NOT mean cleanup is possible: discovery cannot verify the caller's write/delete IAM read-only. That is exactly why credential_steps step 1 is a CANARY that proves deletion works BEFORE any permanent-password, viewer-ranked principal exists. If AdminCreateUser/AdminSetUserPassword/AdminAddUserToGroup/AdminDisableUser/AdminDeleteUser return AccessDenied, stop and record MANUAL_CLI_REVIEW"
fi

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

jq -n \
  --arg region "$REGION" --arg account "$ACCT" --arg caller "$CALLER" \
  --arg api "$API_MATCH" --arg api_why "$API_WHY" \
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
  --arg assets_bucket "$ASSETS_BUCKET" --arg assets_why "$ASSETS_WHY" \
  --argjson assets_confirmed "$ASSETS_CONFIRMED" --argjson bootstrap "$BOOTSTRAP_INFO" \
  --arg rbac_on "$RBAC_ON" --arg no_jwt_role "$NO_JWT_ROLE" \
  --arg user_pool "$USER_POOL" --argjson role_groups "$ROLE_GROUPS" \
  --argjson viewer_members "$VIEWER_MEMBERS" \
  --argjson groups_readable "$GROUPS_READABLE" \
  --argjson members_readable "$MEMBERS_READABLE" \
  --arg discriminator_group "$DISCRIMINATOR_GROUP" \
  --argjson api_config_readable "$API_CONFIG_READABLE" \
  --arg pool_client "$POOL_CLIENT" --argjson auth_flows "$AUTH_FLOWS" \
  --argjson client_has_secret "$CLIENT_HAS_SECRET" --argjson mint_ok "$MINT_OK" \
  --arg pool_mfa "$POOL_MFA" \
  --argjson first_auth_factors "$FIRST_AUTH_FACTORS" \
  --arg password_first_factor "$PASSWORD_FIRST_FACTOR" \
  --arg jwks_url "$JWKS_URL" \
  --arg auth_mode "$AUTH_MODE" --arg auth_iam "$AUTH_IAM" \
  --arg role_verdict "$ROLE_VERDICT" --arg role_why "$ROLE_WHY" '
  {
    region:$region, account:$account, caller_arn:$caller,
    control_plane_api:{
      id:(if $api == "null" then null else $api end),
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
    assets:{
      bucket:$assets_bucket, why:$assets_why, confirmed:$assets_confirmed,
      note:"Confirmed only when the LT bootstrap and the serving Lambda name the same bucket."
    },
    lt_bootstrap:$bootstrap,
    role_identity:{
      rbac_enabled:$rbac_on, default_no_jwt_role:$no_jwt_role,
      user_pool:$user_pool, groups:$role_groups, viewer_members:$viewer_members,
      groups_readable:$groups_readable, members_readable:$members_readable,
      api_config_readable:$api_config_readable,
      mint_capability:{
        app_client:$pool_client, auth_flows:$auth_flows,
        client_has_secret:$client_has_secret, pool_mfa:$pool_mfa, usable:$mint_ok,
        first_auth_factors:$first_auth_factors, password_first_factor:$password_first_factor,
        jwks_url:$jwks_url,
        auth_flow_mode:$auth_mode, discriminator_group:$discriminator_group,
        credential_steps:[
          "1. CANARY FIRST — prove this caller can actually DELETE before it creates anything dangerous: admin-create-user a throwaway that gets NO permanent password and NO group membership, then run the full delete + absence proof on it (see cleanup_contract). Discovery cannot verify cleanup IAM read-only, so this is the only way to find out BEFORE a viewer-ranked, permanent-password principal exists. If the canary cannot be deleted, STOP and record MANUAL_CLI_REVIEW — do not proceed to step 2. Supply an EXPLICIT random --temporary-password (through the same mode-0600 --cli-input-json body, never argv) rather than letting Cognito pick: it generates one for you ONLY when no passwordless option is active on the pool, so on a choice-based pool an omitted value silently creates a PASSWORDLESS user whose credential state is not what you assumed. The canary is a real user either way and is subject to the same cleanup contract",
          "2. admin-create-user --message-action SUPPRESS (username on a reserved unroutable domain, e.g. example.invalid) — the real principal; it is real from this call on, so its delete must already be armed",
          "3. admin-set-user-password --permanent — from here the principal is a live credential; everything after this is on the clock",
          "4. admin-add-user-to-group --group-name viewer",
          "5. admin-list-groups-for-user — the auditable proof of membership; fail closed if it does not contain viewer",
          "6. exchange for an id_token with the flow named in auth_flow_mode: plain -> initiate-auth USER_PASSWORD_AUTH; admin-only -> admin-initiate-auth ADMIN_USER_PASSWORD_AUTH. Capture ONLY --query AuthenticationResult.IdToken; the full AuthenticationResult also carries access and refresh tokens",
          "7. VERIFY the id_token cryptographically before trusting any claim in it — decoding the payload is NOT verification, and a token whose claims are intact but whose SIGNATURE is invalid falls back to role=viewer, producing exactly the 403/200 a passing run produces. Mirror what the runtime does (core/auth.py:_verify_and_decode): fetch mint_capability.jwks_url (public, no secret needed), match the header kid, check the signature, then require iss == the issuer for that pool, aud == the app client id, and token_use == id. Only then read the claims, and from the VERIFIED payload check BOTH: (a) cognito:groups actually contains viewer — a pre-token-generation trigger can rewrite groups, so a principal placed in viewer may still be handed an operator/admin token; (b) exp leaves more remaining lifetime than the whole oracle will take, plus margin. An id_token that expires mid-oracle degrades SILENTLY to the invalid-Bearer path (see step 8) and voids every result after that moment",
          "8. DISCRIMINATING CONTROL, and it is not optional: a viewer 403 alone proves nothing, because an EXPIRED or otherwise unverifiable Bearer also resolves to role=viewer and produces the SAME 403 — and the viewer-OK read still answers 200 either way, so that read is a control for the ROUTE, never for the identity. The only thing that proves the token is being verified at all is a role the fallback cannot produce: while the principal still exists, admin-add-user-to-group --group-name <mint_capability.discriminator_group>, re-login, and require ONE EXACT handler-level response — the specific status AND body marker named by the manifest verification for this patch, e.g. the 400 {code:VALIDATION} that an operator-ranked call gets when the request reaches body validation. Not-403 is NOT the criterion: 401, 404, 429 and every 5xx also satisfy it, so a transient outage or a routing mistake would falsely establish verification. Fail closed on anything other than the one expected response. Then admin-remove-user-from-group for that same group, re-login, and use THAT token for the viewer half. If the elevated attempt still 403s, the token is not being verified and the whole run is vacuous -> MANUAL_CLI_REVIEW",
          "8a. NEGATIVE CONTROL for the discriminator itself: send the same elevated write with NO Authorization header at all and require the RBAC 403 specifically — status 403 AND the rbac body {role, required}. A bare 403 is not enough: an api-key rejection, a WAF rule or a resource policy also answers 403, and accepting those would let a request that never reached the role gate stand in for one that did. If this control does not produce the RBAC 403, the elevated response you just recorded could have come from the header-less fallback rather than from your token — a stripping proxy or a dropped header would look identical. Discovery already BLOCKS the case it can see (DEFAULT_NO_JWT_ROLE outranking viewer), but this control also catches the ones it cannot see",
          "8b. re-run step 7 IN FULL on the FINAL token, signature check included — step 8 replaces the token you validated, and the elevated response authenticated the ELEVATED token, not its replacement. An empty, malformed or badly-signed re-login result falls straight back to role=viewer, producing the expected 403 and a PASS that proves nothing, and on a viewer-only route there is no status code that can tell them apart — which is exactly why the check here has to be cryptographic rather than observational. Require: valid signature against jwks_url, iss/aud/token_use as in step 7, cognito:groups contains viewer, cognito:groups does NOT contain the discriminator group (which also proves the removal actually took effect rather than being read from a cached token), and exp still covers the oracle",
          "9. admin-disable-user, THEN admin-delete-user, then prove it is gone (see cleanup_contract) — disable first so that a delete which fails or lands late leaves an unusable account rather than a live one. Do all of this BEFORE running a long oracle: the token stays valid because the API verifies it cryptographically and never looks the user up. That property is bounded by exp, which is why step 7(b) exists",
          "10. after the oracle, re-check exp against the clock: if it passed mid-run, every result recorded after that point silently came from the invalid-Bearer fallback, not from your viewer principal — discard them and record MANUAL_CLI_REVIEW rather than reporting a PASS"
        ],
        iam_unverified:("discovery does not verify the caller holds the actions these steps need: cognito-idp:AdminCreateUser, AdminSetUserPassword, AdminAddUserToGroup, AdminRemoveUserFromGroup (step 8 puts the principal in " + (if $discriminator_group == "" then "the discriminator group" else $discriminator_group end) + " and must take it back out), AdminListGroupsForUser (the membership audit fails closed without it), AdminGetUser (the eventual-consistency absence probe), AdminDisableUser (the leftover is made unusable before the delete), AdminDeleteUser; plus ListUsersInGroup and AdminGetUser again for the mandatory post-run re-check. " + $auth_iam + ". A caller granted less will fail AFTER the principal has been created and password-enabled, which is the worst place to fail. On AccessDenied stop and record MANUAL_CLI_REVIEW"),
        cleanup_contract:[
          "the principal carries a PERMANENT password: a leftover IS a live credential in the customer account, so cleanup is not best-effort",
          "this contract applies to the step-1 canary too: admin-create-user generates a temporary password even when none is supplied, so the canary is a real user, not a dry run",
          "arm the delete BEFORE the create call, and treat a lost create response as may-exist — that failure mode is exactly the one that would otherwise skip cleanup",
          "UsernameExistsException means the name predates this run: never delete it",
          "route every catchable fatal signal through exit so a single cleanup path runs once; a default-disposition kill skips traps entirely",
          "admin-disable-user BEFORE admin-delete-user: a delete that fails, or that lands after the run has ended, then leaves an UNUSABLE account instead of a live one",
          "run the delete with retries disabled: a retried delete answers UserNotFoundException and would make a successful cleanup look like a failure",
          "these two UserNotFoundException rules are DIFFERENT calls, do not merge them: (a) from the DELETE call itself, after a confirmed create, it is NOT proof of success — it can only be a stale read, i.e. the delete did not happen, so treat it as a failed delete; (b) from admin-get-user AFTER a delete that reported success, it is the absence signal you are looking for",
          "prove absence with SEVERAL time-separated admin-get-user reads that all raise UserNotFoundException; the time separation IS the evidence, so back-to-back reads prove nothing",
          "a failed delete must exit NON-ZERO with a BLOCKING line; printing a warning and returning success blocks nothing",
          "keep the password out of argv and the environment (use --cli-input-json from a mode-0600 file): /proc/<pid>/cmdline is world-readable",
          "the id_token is the SECOND secret and it does not die with the principal: it stays usable until exp even after admin-delete-user succeeds, because the API verifies it cryptographically and never looks the user up. So keep it out of argv too (pass the Authorization header from a file or stdin, not on a command line) and destroy it like a credential, not like a variable",
          "create BOTH secret files (the --cli-input-json password body and the token/header file) inside one private mode-0700 temporary directory that this run made, and destroy that directory through the SAME signal-safe cleanup path on EVERY exit — including the failure paths. A cleanup that gives up after a failed delete must still remove them: otherwise the one run that leaves a live principal behind also leaves its permanent password on disk",
          "post-run, re-check with admin-get-user and confirm the viewer group member count returned to its pre-run value"
        ],
        cleanup_contract_note:"this list is a CONTRACT, not a snippet. It is a program: if you automate it, hold the automation to every line above. A hand-written inline version of this failed review on the delete-result check, signal handling, argv leakage and quoting — see the changelog for #396.",
      },
      verdict:$role_verdict, why:$role_why,
      viewer_paths:[
        "cognito-group (auditable, real principal; needs mint_capability.usable): follow mint_capability.credential_steps in order, and hold the run to every line of mint_capability.cleanup_contract. The membership proof is admin-list-groups-for-user, which is what makes this source auditable.",
        "invalid-bearer (zero mutation, no Cognito needed): any non-verifiable Bearer resolves to role=viewer regardless of DEFAULT_NO_JWT_ROLE. Proves the gate denies viewer rank; does NOT prove a real viewer principal exists."
      ],
      anti_pattern:("never omit Authorization to mean viewer: that path resolves to DEFAULT_NO_JWT_ROLE (here: " + $no_jwt_role + ") AND sets is_admin=true for ownership checks, so on a trusted-automation deployment it is an admin call"),
      note:"VACUOUS or BLOCKED means a role oracle must be recorded MANUAL_CLI_REVIEW, not PASS. viewer_members is informational only — an existing member is not a usable credential because its password is not held."
    },
    dispatch_mode:$dispatch, fix_applicability:$fixes
  }' > "$OUT"

{
  echo
  echo "================ CONFIRM BEFORE APPLY ================"
  echo "account/region : $ACCT / $REGION"
  echo "caller         : $CALLER"
  echo "API confirmed  : $API_MATCH ($API_WHY)"
  printf '%s' "$API_FACTS" | jq -r '
    .[] | "  API \(.id) \(.name): tenants=\(.has_tenants) hosts=\(.has_hosts) " +
    "proxy=\(.proxy) auth=\(.method_auth) key=\(.api_key_required) policy=\(.resource_policy)"'
  echo "API integration: ${API_INTEGRATION_TARGET:-<unresolved>}"
  echo "Lambda aliases : $(printf '%s' "$API_ALIASES" | jq -c '.')"
  echo "dispatch ESM   : ${DISPATCH_ESM_TARGET:-<unresolved>}"
  echo "HOST ASG       : ${ASG_NAME:-<unresolved>} type=$ASG_TYPE LT=${ASG_LT_NAME:-$ASG_LT_ID} v=$ASG_LT_VER candidates=$ASG_CANDIDATE_N"
  echo "LT bootstrap   : $(printf '%s' "$BOOTSTRAP_INFO" | jq -r '.form // "<unclassified>"')"
  echo "assets bucket  : ${ASSETS_BUCKET:-<unresolved>} confirmed=$ASSETS_CONFIRMED ($ASSETS_WHY)"
  echo "hosts          : $(printf '%s' "$HOST_IDS" | jq 'length') ($HOSTS_TABLE)"
  echo "role identity  : $ROLE_VERDICT (rbac_enabled=$RBAC_ON no_jwt_role=$NO_JWT_ROLE groups=$ROLE_GROUPS viewer_members=$VIEWER_MEMBERS mint=$MINT_OK)"
  echo "                 $ROLE_WHY"
  echo "fix applicability:"
  printf '%s' "$FIX_VERDICTS" |
    jq -r '.[] | "  \(.id): \(.verdict) [\(.applies_when)]"'
  echo
  echo "[ ] API: confirmed must be true; otherwise do not run route apply."
  echo "[ ] ASG: confirmed must be true; otherwise do not run LT apply."
  echo "[ ] assets: on an s3-bootstrap LT, confirmed must be true; otherwise do not run LT apply."
  echo "[ ] role oracles: role_identity.verdict must be OBTAINABLE; VACUOUS or"
  echo "    BLOCKED means record MANUAL_CLI_REVIEW — never PASS a role check you cannot make."
  echo "written: $OUT (discovery made no AWS changes)"
  echo "======================================================"
} >&2

echo "$OUT"
