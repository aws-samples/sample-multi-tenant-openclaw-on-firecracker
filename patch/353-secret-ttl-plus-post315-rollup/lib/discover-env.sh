#!/usr/bin/env bash
# discover-env.sh — the DISCOVER phase, made deterministic. READ-ONLY probe of the target environment
# that writes environment.json AND prints every discovered value for the operator to CONFIRM before
# any apply. The point: environment adaptation (resource names, the right API, the ASG-pinned LT
# version, which fixes apply) is NOT typed by a human from memory — it's machine-probed, shown, and
# confirmed, so every executor (human or LLM) proceeds from the SAME real values, not a guess.
#
# It NEVER writes to AWS: only describe/list/get. Run it first; read the printed CONFIRM block; only
# if every line matches your intent do you proceed to APPLY-INSTRUCTIONS.md.
#
# Usage:  discover-env.sh <region> [manifest.json]     # manifest defaults to ../manifest.json
# Output: environment.json (next to the kit) + a human CONFIRM block on stderr.
set -euo pipefail

[ $# -ge 1 ] || { echo "usage: discover-env.sh <region> [manifest.json]" >&2; exit 2; }
REGION="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${2:-$HERE/../manifest.json}"
OUT="$HERE/../environment.json"
_need() { command -v "$1" >/dev/null || { echo "FATAL: need '$1'" >&2; exit 1; }; }
_need aws; _need jq
Q() { aws "$@" --region "$REGION" --output json; }
warn() { echo "  [!] $*" >&2; }

echo "== discover-env (READ-ONLY) region=$REGION ==" >&2

ACCT="$(aws sts get-caller-identity --query Account --output text)"
CALLER="$(aws sts get-caller-identity --query Arn --output text)"

# --- rule 9: pick the control-plane API by BEHAVIOR, not name ---------------------------------------
# The right one is a NON-proxy REST API that has explicit /tenants and /hosts resources. A /{proxy+}
# API (often the one literally named "-private") is the trap. We list candidates + flag the match;
# the operator still confirms (a host-SSM GET /tenants 200 is the final proof, printed as a follow-up).
API_CANDIDATES="$(Q apigateway get-rest-apis \
  | jq -c '[.items[] | {id, name}]')"
API_MATCH="null"; API_WHY=""
for id in $(printf '%s' "$API_CANDIDATES" | jq -r '.[].id'); do
  res="$(Q apigateway get-resources --rest-api-id "$id" | jq -r '[.items[].path]')"
  has_tenants=$(printf '%s' "$res" | jq 'any(.[]; . == "/tenants" or startswith("/tenants"))')
  has_hosts=$(printf '%s' "$res" | jq 'any(.[]; . == "/hosts" or startswith("/hosts"))')
  has_proxy=$(printf '%s' "$res" | jq 'any(.[]; contains("{proxy+}"))')
  if [ "$has_tenants" = true ] && [ "$has_hosts" = true ] && [ "$has_proxy" = false ]; then
    API_MATCH="$id"; API_WHY="explicit /tenants + /hosts, no {proxy+}"; break
  fi
done
[ "$API_MATCH" = null ] && warn "no non-proxy /tenants+/hosts API auto-matched — pick manually + host-SSM-probe GET /tenants==200"

# --- which Lambda LINK is actually SERVING (315 real-run: API GW -> live ALIAS; dispatch SQS ESM ->
#     $LATEST; they diverge). A code update must hit BOTH the alias the API invokes AND $LATEST, or the
#     patch lands on a Lambda version nothing is serving. Sense them so the operator patches the LIVE
#     link, not a stale alias. ---------------------------------------------------------------------
API_DEPLOYED_STAGES="[]"; API_INTEGRATION_TARGET=""
if [ "$API_MATCH" != null ]; then
  API_DEPLOYED_STAGES="$(Q apigateway get-stages --rest-api-id "$API_MATCH" | jq -c '[.item[] | {stage:.stageName, deployed:.lastUpdatedDate}]')"
  # the ARN the API's methods integrate to (reveals :alias suffix if the API invokes an alias)
  RID="$(Q apigateway get-resources --rest-api-id "$API_MATCH" | jq -r '[.items[] | select(.resourceMethods)] | .[0].id // ""')"
  [ -n "$RID" ] && API_INTEGRATION_TARGET="$(Q apigateway get-integration --rest-api-id "$API_MATCH" --resource-id "$RID" --http-method ANY 2>/dev/null | jq -r '.uri // ""' | grep -oE 'function:[^/]+' | head -1)" || true
fi
# openclaw-api alias lineage: which alias points at which version (API GW invokes ONE of these).
API_ALIASES="$(Q lambda list-aliases --function-name openclaw-api 2>/dev/null | jq -c '[.Aliases[] | {alias:.Name, version:.FunctionVersion}]')" || API_ALIASES="[]"
API_LATEST_SHA="$(Q lambda get-function-configuration --function-name openclaw-api --qualifier '$LATEST' 2>/dev/null | jq -r '.CodeSha256 // ""')" || API_LATEST_SHA=""
# dispatch SQS event-source-mapping: which qualifier does it bind ($LATEST vs an alias)?
DISPATCH_ESM_TARGET="$(Q lambda list-event-source-mappings --function-name openclaw-api 2>/dev/null | jq -r '[.EventSourceMappings[] | select(.EventSourceArn|test("sqs";"i")) | .FunctionArn] | .[0] // ""' | grep -oE '(:[0-9]+|:[A-Za-z0-9_-]+)$' | tr -d ':' | head -1)" || DISPATCH_ESM_TARGET=""
[ -z "$API_INTEGRATION_TARGET" ] && warn "could not read the API's Lambda integration target — confirm which alias the API GW invokes before update-function-code"

# --- ASG + the LT version it ACTUALLY pins (never assume $Default) -----------------------------------
ASG_JSON="$(Q autoscaling describe-auto-scaling-groups | jq -c '[.AutoScalingGroups[] | select(.AutoScalingGroupName|test("openclaw|host";"i"))]')"
ASG_NAME="$(printf '%s' "$ASG_JSON" | jq -r '.[0].AutoScalingGroupName // ""')"
ASG_LT="$(printf '%s' "$ASG_JSON" | jq -c '.[0] | (.MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification // .LaunchTemplate // {})')"
ASG_LT_ID="$(printf '%s' "$ASG_LT" | jq -r '.LaunchTemplateId // ""')"
ASG_LT_VER="$(printf '%s' "$ASG_LT" | jq -r '.Version // ""')"
ASG_IS_MIP="$(printf '%s' "$ASG_JSON" | jq -r 'if .[0].MixedInstancesPolicy then "mip" else "plain" end')"
case "$ASG_LT_VER" in '$Latest'|'$Default') warn "ASG pins '$ASG_LT_VER' (floating) — pin a concrete numeric version before an LT roll (apply-lt.sh refuses floating)";; esac

# --- live host instance-ids (from the hosts table = what the scheduler sees) ------------------------
HOSTS_TABLE="$(Q dynamodb list-tables | jq -r '.TableNames[] | select(test("openclaw-hosts"))' | head -1)"
HOST_IDS="[]"
[ -n "$HOSTS_TABLE" ] && HOST_IDS="$(Q dynamodb scan --table-name "$HOSTS_TABLE" --projection-expression instance_id \
  | jq -c '[.Items[].instance_id.S]')"

# --- per-fix applies_when verdicts: probe the real config value each fix is gated on -----------------
# We read the runtime signals the manifest's applies_when clauses reference and print a verdict per fix,
# so the operator sees which fixes are IN-SCOPE for THIS environment (a fix gated on logging.enabled
# on a no-logging deployment should be skipped, not forced).
DISPATCH_MODE="$(Q lambda get-function-configuration --function-name openclaw-api 2>/dev/null | jq -r '.Environment.Variables.DISPATCH_MODE // "unknown"')" || DISPATCH_MODE=unknown
LOGGING_ON="$( [ -n "$HOSTS_TABLE" ] && Q dynamodb list-tables | jq -r 'if any(.TableNames[]; test("observability|opensearch")) then "likely-true" else "unknown" end' || echo unknown )"
FIX_VERDICTS="$(jq -c --arg dm "$DISPATCH_MODE" --arg log "$LOGGING_ON" '
  [ .fixes[] | {id, applies_when,
    verdict: ( if (.applies_when|test("dispatch.mode==ddb")) then (if $dm=="ddb" then "IN-SCOPE" else "CHECK (dispatch.mode="+$dm+")" end)
               elif (.applies_when|test("logging.enabled==true")) then ("CHECK (logging="+$log+")")
               else "IN-SCOPE (always)" end) } ]' "$MANIFEST")"

# --- write environment.json (the machine record) ----------------------------------------------------
jq -n --arg region "$REGION" --arg acct "$ACCT" --arg caller "$CALLER" \
      --arg api "$API_MATCH" --arg apiwhy "$API_WHY" --argjson apicands "$API_CANDIDATES" \
      --argjson stages "$API_DEPLOYED_STAGES" --arg apitarget "$API_INTEGRATION_TARGET" \
      --argjson aliases "$API_ALIASES" --arg latestsha "$API_LATEST_SHA" --arg esm "$DISPATCH_ESM_TARGET" \
      --arg asg "$ASG_NAME" --arg ltid "$ASG_LT_ID" --arg ltver "$ASG_LT_VER" --arg ismip "$ASG_IS_MIP" \
      --arg htable "$HOSTS_TABLE" --argjson hosts "$HOST_IDS" --argjson fixes "$FIX_VERDICTS" \
      --arg dm "$DISPATCH_MODE" \
  '{region:$region, account:$acct, caller_arn:$caller,
    control_plane_api:{id:$api, why:$apiwhy, candidates:$apicands, deployed_stages:$stages,
                       lambda_integration:$apitarget, note:"confirm with a host-SSM GET /tenants==200"},
    lambda_link:{function:"openclaw-api", api_invokes_alias:$apitarget, aliases:$aliases,
                 latest_code_sha256:$latestsha, dispatch_sqs_esm_binds:$esm,
                 note:"update-function-code must hit BOTH the API-invoked alias AND $LATEST (dispatch ESM binds $LATEST) — 315 real-run"},
    asg:{name:$asg, lt_id:$ltid, lt_version_pinned:$ltver, type:$ismip},
    hosts:{table:$htable, instance_ids:$hosts, count:($hosts|length)},
    dispatch_mode:$dm, fix_applicability:$fixes}' > "$OUT"

# --- CONFIRM block: everything the operator must eyeball before applying ----------------------------
{
  echo; echo "======================= CONFIRM BEFORE APPLY (read every line) ======================="
  echo "account/region : $ACCT / $REGION   caller: $CALLER"
  echo "control-plane API: ${API_MATCH}  ($API_WHY)   << rule 9: confirm host-SSM GET /tenants == 200"
  echo "  API stages     : $(printf '%s' "$API_DEPLOYED_STAGES" | jq -rc '[.[].stage]')   integrates -> ${API_INTEGRATION_TARGET:-<unknown>}"
  echo "LIVE Lambda link : openclaw-api  API invokes -> ${API_INTEGRATION_TARGET:-<confirm!>}"
  echo "  aliases        : $(printf '%s' "$API_ALIASES" | jq -rc '.')   \$LATEST sha=${API_LATEST_SHA:0:12}"
  echo "  dispatch ESM   : SQS binds -> ${DISPATCH_ESM_TARGET:-\$LATEST?}   << update-function-code must hit BOTH the API alias AND \$LATEST"
  echo "ASG            : ${ASG_NAME:-<none found>}   LT id=$ASG_LT_ID version=$ASG_LT_VER ($ASG_IS_MIP)"
  echo "hosts          : $(printf '%s' "$HOST_IDS" | jq -r 'length') live (table $HOSTS_TABLE)"
  echo "dispatch.mode  : $DISPATCH_MODE"
  echo "fix applicability (skip a CHECK/out-of-scope fix, don't force it):"
  printf '%s' "$FIX_VERDICTS" | jq -r '.[] | "   \(.id): \(.verdict)   [\(.applies_when)]"'
  echo "written to     : $OUT   (READ-ONLY probe — nothing on AWS was changed)"
  echo "If any line is wrong, STOP and fix your target/credentials before touching APPLY-INSTRUCTIONS.md."
  echo "======================================================================================="
} >&2
echo "$OUT"
