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

# --- control-plane API: REPORT candidates + their auth facts; let the OPERATOR confirm --------------
# Do NOT hard-code which API is "right". Deployments differ: the control plane may be a proxy
# (/{proxy+}) API or one with explicit /tenants,/hosts routes; its methods may use AWS_IAM (SigV4),
# API keys, a custom authorizer, or NONE; access may be fenced by a resource policy (e.g. VPCE-only)
# rather than method auth. A route-shape heuristic alone picks the wrong one (real-run: the explicit
# -routes API and the proxy API can swap roles between environments). So we list EVERY REST API with
# the facts an operator needs — route shape, per-method authorizationType/apiKeyRequired, and whether
# a resource policy restricts it — and let them match it to how THEY actually call it (config like
# PRIVATE_API_URL / CTRL_API_BASE + the auth their client sends). The winner is confirmed by a real
# call from the real call site returning 200, using the SAME auth the client uses (SigV4, api key, or
# whatever the resource policy allows) — NOT by a shape guess and NOT by assuming SigV4.
API_CANDIDATES="$(Q apigateway get-rest-apis | jq -c '[.items[] | {id, name}]')"
API_FACTS="[]"
for id in $(printf '%s' "$API_CANDIDATES" | jq -r '.[].id'); do
  nm="$(printf '%s' "$API_CANDIDATES" | jq -r --arg id "$id" '(.[]|select(.id==$id)).name // "?"')"
  res_json="$(Q apigateway get-resources --rest-api-id "$id" 2>/dev/null || echo '{"items":[]}')"
  res="$(printf '%s' "$res_json" | jq -r '[.items[].path]')"
  has_tenants=$(printf '%s' "$res" | jq 'any(.[]; . == "/tenants" or startswith("/tenants"))')
  has_hosts=$(printf '%s' "$res" | jq 'any(.[]; . == "/hosts" or startswith("/hosts"))')
  has_proxy=$(printf '%s' "$res" | jq 'any(.[]; contains("{proxy+}"))')
  # sample the auth on the first resource that has methods (proxy ANY, or an explicit route)
  rid="$(printf '%s' "$res_json" | jq -r '[.items[] | select(.resourceMethods)] | .[0].id // ""')"
  meth="$(printf '%s' "$res_json" | jq -r 'first(.items[] | select(.resourceMethods) | .resourceMethods | keys[] | select(. != "OPTIONS")) // ""')"
  auth="unknown"; apikey="unknown"
  if [ -n "$rid" ] && [ -n "$meth" ]; then
    am="$(Q apigateway get-method --rest-api-id "$id" --resource-id "$rid" --http-method "$meth" 2>/dev/null | jq -c '{auth:.authorizationType, apikey:.apiKeyRequired}')" || am=""
    [ -n "$am" ] && { auth="$(printf '%s' "$am" | jq -r '.auth // "unknown"')"; apikey="$(printf '%s' "$am" | jq -r '.apikey // "unknown"')"; }
  fi
  # resource policy present? (VPCE-only fencing lives here, not in method auth)
  has_respolicy="$(Q apigateway get-rest-api --rest-api-id "$id" 2>/dev/null | jq -r 'if (.policy // "") != "" then "yes" else "no" end')" || has_respolicy=unknown
  API_FACTS="$(printf '%s' "$API_FACTS" | jq -c \
    --arg id "$id" --arg nm "$nm" --argjson t "$has_tenants" --argjson h "$has_hosts" --argjson p "$has_proxy" \
    --arg au "$auth" --arg ak "$apikey" --arg rp "$has_respolicy" \
    '. + [{id:$id, name:$nm, has_tenants:$t, has_hosts:$h, proxy:$p, method_auth:$au, api_key_required:$ak, resource_policy:$rp}]')"
done
# A neutral SUGGESTION only: an API that can serve control-plane calls (explicit /tenants+/hosts OR a
# proxy that forwards them). If exactly one, suggest it; otherwise leave null and make the operator pick.
API_SUGGESTED="$(printf '%s' "$API_FACTS" | jq -c '[.[] | select((.has_tenants and .has_hosts) or .proxy)]')"
API_SUGGEST_N="$(printf '%s' "$API_SUGGESTED" | jq 'length')"
if [ "$API_SUGGEST_N" = 1 ]; then
  API_MATCH="$(printf '%s' "$API_SUGGESTED" | jq -r '.[0].id')"
  API_WHY="only candidate that can serve control-plane routes — UNCONFIRMED (verify with a real call from your call site, using your client's auth)"
else
  API_MATCH="null"
  API_WHY="$API_SUGGEST_N candidates could serve the control plane — operator must confirm (do NOT guess by route shape); pick the one your config (PRIVATE_API_URL/CTRL_API_BASE) points to AND that answers a real call with your auth"
  warn "control-plane API not auto-resolved ($API_SUGGEST_N candidates). Match it to how YOU call it (your config + your auth: SigV4 / api key / resource-policy VPCE), not to route shape. Candidates + auth facts:"
  printf '%s' "$API_FACTS" | jq -r '.[] | "      - \(.id) (\(.name))  tenants=\(.has_tenants) hosts=\(.has_hosts) proxy=\(.proxy)  method_auth=\(.method_auth) api_key=\(.api_key_required) resource_policy=\(.resource_policy)"' >&2
fi

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

# --- the HOST ASG + the LT version it ACTUALLY pins (never assume $Default) --------------------------
# CRITICAL (real-run bug): this patch targets the FIRECRACKER HOST fleet only (#331 launch-vm slots,
# #323 host-agent KillMode in the LT userdata, #321 disk-GC). A deployment usually has TWO matching
# ASGs — the OpenResty EDGE fleet (openclaw-edge-asg / openclaw-edge-lt) and the host fleet
# (openclaw-hosts-asg / openclaw-host-lt). Selecting by a loose name regex and taking [0] can pick the
# EDGE ASG and silently roll host userdata onto the wrong fleet (apply-lt.sh's floating-version guard
# does NOT catch a wrong-but-concrete target). So: identify the host fleet by identity, require a
# UNIQUE match, and refuse to guess. The operator can override with OC_HOST_ASG_NAME.
ASG_ALL="$(Q autoscaling describe-auto-scaling-groups | jq -c '[.AutoScalingGroups[] | {name:.AutoScalingGroupName, lt:((.MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification // .LaunchTemplate // {}))}]')"
# 1) explicit override wins (deterministic escape hatch).
if [ -n "${OC_HOST_ASG_NAME:-}" ]; then
  ASG_CANDS="$(printf '%s' "$ASG_ALL" | jq -c --arg n "$OC_HOST_ASG_NAME" '[.[] | select(.name==$n)]')"
  ASG_WHY="OC_HOST_ASG_NAME override"
else
  # 2) prefer host identity, NEVER match "edge": ASG name or its LT name says host, and NOT edge.
  #    (LT-name signal is the reliable discriminator: openclaw-host-lt vs openclaw-edge-lt.)
  ASG_CANDS="$(printf '%s' "$ASG_ALL" | jq -c '[.[]
    | . as $a
    | (.lt.LaunchTemplateName // "") as $ltn
    | select( (($a.name|test("host";"i")) or ($ltn|test("host";"i")))
              and (($a.name|test("edge";"i"))|not) and (($ltn|test("edge";"i"))|not) )]')"
  ASG_WHY="name/LT says host, not edge"
fi
ASG_CANDS_N="$(printf '%s' "$ASG_CANDS" | jq 'length')"
ASG_NAME=""; ASG_LT_ID=""; ASG_LT_VER=""; ASG_IS_MIP="plain"
if [ "$ASG_CANDS_N" = 1 ]; then
  ASG_NAME="$(printf '%s' "$ASG_CANDS" | jq -r '.[0].name')"
  ASG_LT_ID="$(printf '%s' "$ASG_CANDS" | jq -r '.[0].lt.LaunchTemplateId // ""')"
  ASG_LT_VER="$(printf '%s' "$ASG_CANDS" | jq -r '.[0].lt.Version // ""')"
  ASG_IS_MIP="$(printf '%s' "$ASG_ALL" | jq -r --arg n "$ASG_NAME" '(.[]|select(.name==$n)) as $x | "plain"')"
  # verify the pinned LT really is a HOST template, not edge (defense in depth vs a mis-named ASG).
  LTN="$(Q ec2 describe-launch-templates --launch-template-ids "$ASG_LT_ID" 2>/dev/null | jq -r '.LaunchTemplates[0].LaunchTemplateName // ""')" || LTN=""
  case "$LTN" in *edge*) warn "SELECTED ASG '$ASG_NAME' pins LT '$LTN' which looks like the EDGE template — STOP and set OC_HOST_ASG_NAME to the host fleet before running apply-lt.sh";; esac
  ASG_LT_NAME="$LTN"
else
  # 0 or >1: do NOT guess. Print every candidate and force the operator to disambiguate.
  ASG_WHY="AMBIGUOUS ($ASG_CANDS_N candidates) — set OC_HOST_ASG_NAME"
  warn "host ASG not uniquely identified ($ASG_CANDS_N candidates matched '$ASG_WHY'). Do NOT run apply-lt.sh until resolved — re-run with OC_HOST_ASG_NAME=<host-fleet-asg>. Candidates below:"
  printf '%s' "$ASG_ALL" | jq -r '.[] | "      - \(.name)  LT=\(.lt.LaunchTemplateName // .lt.LaunchTemplateId // "?") v\(.lt.Version // "?")"' >&2
fi
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
      --argjson apifacts "$API_FACTS" \
      --argjson stages "$API_DEPLOYED_STAGES" --arg apitarget "$API_INTEGRATION_TARGET" \
      --argjson aliases "$API_ALIASES" --arg latestsha "$API_LATEST_SHA" --arg esm "$DISPATCH_ESM_TARGET" \
      --arg asg "$ASG_NAME" --arg ltid "$ASG_LT_ID" --arg ltver "$ASG_LT_VER" --arg ismip "$ASG_IS_MIP" \
      --arg htable "$HOSTS_TABLE" --argjson hosts "$HOST_IDS" --argjson fixes "$FIX_VERDICTS" \
      --arg dm "$DISPATCH_MODE" \
      --arg ltname "${ASG_LT_NAME:-}" --arg asgn "$ASG_CANDS_N" --arg apin "$API_SUGGEST_N" \
  '{region:$region, account:$acct, caller_arn:$caller,
    control_plane_api:{id:$api, why:$apiwhy, candidates:$apicands, candidate_facts:$apifacts,
                       deployed_stages:$stages, lambda_integration:$apitarget, candidate_count:($apin|tonumber),
                       confirmed:false, note:"SUGGESTION ONLY, not confirmed. Pick the API your own config (PRIVATE_API_URL/CTRL_API_BASE) points to AND that answers a real call from your call site using YOUR auth (SigV4 / api key / resource-policy VPCE — see candidate_facts[].method_auth+resource_policy). Set confirmed:true only after that call succeeds."},
    lambda_link:{function:"openclaw-api", api_invokes_alias:$apitarget, aliases:$aliases,
                 latest_code_sha256:$latestsha, dispatch_sqs_esm_binds:$esm,
                 note:"update-function-code must hit BOTH the API-invoked alias AND $LATEST (dispatch ESM binds $LATEST) — 315 real-run"},
    asg:{name:$asg, lt_id:$ltid, lt_name:$ltname, lt_version_pinned:$ltver, type:$ismip,
         candidate_count:($asgn|tonumber),
         note:"HOST fleet only. If name/lt_name is empty or count!=1 this is UNRESOLVED — set OC_HOST_ASG_NAME and re-run; NEVER run apply-lt.sh against an edge LT"},
    hosts:{table:$htable, instance_ids:$hosts, count:($hosts|length)},
    dispatch_mode:$dm, fix_applicability:$fixes}' > "$OUT"

# --- CONFIRM block: everything the operator must eyeball before applying ----------------------------
{
  echo; echo "======================= CONFIRM BEFORE APPLY (read every line) ======================="
  echo "account/region : $ACCT / $REGION   caller: $CALLER"
  echo "control-plane API: ${API_MATCH}  ($API_WHY)"
  echo "  candidates + auth (match to how YOU call it — config + your auth, NOT route shape):"
  printf '%s' "$API_FACTS" | jq -r '.[] | "     - \(.id) (\(.name))  tenants=\(.has_tenants) hosts=\(.has_hosts) proxy=\(.proxy)  method_auth=\(.method_auth) api_key=\(.api_key_required) resource_policy=\(.resource_policy)"'
  echo "  API stages     : $(printf '%s' "$API_DEPLOYED_STAGES" | jq -rc '[.[].stage]')   integrates -> ${API_INTEGRATION_TARGET:-<unknown>}"
  echo "LIVE Lambda link : openclaw-api  API invokes -> ${API_INTEGRATION_TARGET:-<confirm!>}"
  echo "  aliases        : $(printf '%s' "$API_ALIASES" | jq -rc '.')   \$LATEST sha=${API_LATEST_SHA:0:12}"
  echo "  dispatch ESM   : SQS binds -> ${DISPATCH_ESM_TARGET:-\$LATEST?}   << update-function-code must hit BOTH the API alias AND \$LATEST"
  echo "ASG (HOST fleet): ${ASG_NAME:-<UNRESOLVED>}   LT ${ASG_LT_NAME:-$ASG_LT_ID} version=$ASG_LT_VER ($ASG_IS_MIP)   [candidates=$ASG_CANDS_N]"
  echo "hosts          : $(printf '%s' "$HOST_IDS" | jq -r 'length') live (table $HOSTS_TABLE)"
  echo "dispatch.mode  : $DISPATCH_MODE"
  echo "fix applicability (skip a CHECK/out-of-scope fix, don't force it):"
  printf '%s' "$FIX_VERDICTS" | jq -r '.[] | "   \(.id): \(.verdict)   [\(.applies_when)]"'
  echo "written to     : $OUT   (READ-ONLY probe — nothing on AWS was changed)"
  echo
  echo "--- TWO HARD GATES before you run apply-lt.sh / update-function-code (do NOT skip) ---"
  echo " [ ] 1. control-plane API confirmed: from your real call site, call the API your config"
  echo "        (PRIVATE_API_URL / CTRL_API_BASE) points to, using YOUR auth (SigV4, api key, or"
  echo "        whatever its resource_policy/method_auth requires — see the candidates line above)."
  echo "        It must answer (e.g. 200 on GET /tenants). A 403 usually means WRONG api OR wrong auth"
  echo "        for THAT api ($API_SUGGEST_N candidate(s) can serve — route shape alone does NOT prove it)."
  if [ "${ASG_CANDS_N:-0}" != 1 ]; then
    echo " [ ] 2. HOST ASG UNRESOLVED ($ASG_CANDS_N candidates). apply-lt.sh would target the WRONG fleet."
    echo "        Re-run:  OC_HOST_ASG_NAME=<host-fleet-asg> discover-env.sh $REGION"
  else
    echo " [ ] 2. HOST ASG = '$ASG_NAME' pins LT '${ASG_LT_NAME:-$ASG_LT_ID}'. Confirm this is the FIRECRACKER"
    echo "        host fleet, NOT the edge (OpenResty) fleet — host userdata on the edge LT breaks edge."
  fi
  echo "If any line is wrong, STOP and fix your target/credentials before touching APPLY-INSTRUCTIONS.md."
  echo "======================================================================================="
} >&2
echo "$OUT"
