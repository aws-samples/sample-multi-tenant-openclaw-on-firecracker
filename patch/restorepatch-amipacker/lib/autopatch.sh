#!/usr/bin/env bash
# autopatch.sh - execute a recorded patch decision without phase-by-phase prompts.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INTERVIEW="$HERE/interview-once.py"
APPLY="$HERE/apply-restorepatch.sh"
DISCOVER="$HERE/discover-env.sh"

say() { echo "== $*"; }
warn() { echo "  [!] $*" >&2; }
die() { echo "FAIL: $*" >&2; exit 1; }

usage() {
  echo "usage: autopatch.sh <kit-dir> [--answers <answers.json>] [--region <region>] [--env <environment.json>] [--yes]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
KIT_INPUT="$1"
shift
ANSWERS_FILE=""
REGION=""
ENV_INPUT=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --answers) [ $# -ge 2 ] || usage; ANSWERS_FILE="$2"; shift 2 ;;
    --region) [ $# -ge 2 ] || usage; REGION="$2"; shift 2 ;;
    --env) [ $# -ge 2 ] || usage; ENV_INPUT="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    *) usage ;;
  esac
done

[ -d "$KIT_INPUT" ] || die "kit directory not found: $KIT_INPUT"
KITDIR="$(cd "$KIT_INPUT" && pwd)"
MANIFEST="$KITDIR/manifest.json"
DECISION="$KITDIR/DECISION.json"
DEFAULT_ENV="$KITDIR/environment.json"
if [ -n "$ENV_INPUT" ]; then
  ENV_PARENT="$(cd "$(dirname "$ENV_INPUT")" && pwd)"
  ENVJSON="$ENV_PARENT/$(basename "$ENV_INPUT")"
else
  ENVJSON="$DEFAULT_ENV"
fi

[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
[ -f "$INTERVIEW" ] || die "interview helper not found: $INTERVIEW"
[ -f "$APPLY" ] || die "apply driver not found: $APPLY"
[ -f "$DISCOVER" ] || die "discovery helper not found: $DISCOVER"

TEMP_PATHS=""
RECEIPT=""
FINALIZED=0
FIRST_WRITE=0
ROUTE_STARTED=0
LAST_LOG=""
CURRENT_STEP=""

remember_temp() {
  TEMP_PATHS="${TEMP_PATHS}${TEMP_PATHS:+
}$1"
}

cleanup_temps() {
  [ -n "$TEMP_PATHS" ] || return 0
  printf '%s\n' "$TEMP_PATHS" | python3 -c '
import os
import sys
for raw in sys.stdin:
    path = raw.rstrip("\n")
    if not path:
        continue
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
' >/dev/null 2>&1
}

shell_command() {
  local output="" value
  for value in "$@"; do
    printf -v value '%q' "$value"
    output="${output}${output:+ }${value}"
  done
  printf '%s' "$output"
}

receipt_update() {
  local filter="$1" tmp
  shift
  [ -n "$RECEIPT" ] && [ -f "$RECEIPT" ] || return 0
  tmp="$(mktemp "${TMPDIR:-/tmp}/autopatch-receipt.XXXXXX")"
  remember_temp "$tmp"
  jq "$@" "$filter" "$RECEIPT" > "$tmp" || return 1
  mv "$tmp" "$RECEIPT"
}

record_step() {
  local name="$1" command="$2" code="$3" timestamp="$4"
  receipt_update \
    '.steps += [{name:$name,command:$command,exit_code:$code,timestamp:$timestamp,status:(if $code == 0 then "RAN" else "FAILED" end)}]' \
    --arg name "$name" --arg command "$command" --argjson code "$code" \
    --arg timestamp "$timestamp"
}

record_skip() {
  local name="$1" command="$2" reason="$3"
  receipt_update \
    '.steps += [{name:$name,command:$command,exit_code:null,timestamp:$timestamp,status:"SKIPPED",reason:$reason}]' \
    --arg name "$name" --arg command "$command" --arg reason "$reason" \
    --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

annotate_overlay() {
  local name="$1" functions="$2"
  receipt_update \
    '(.steps | map(select(.name == $name)) | last) as $target
     | if $target == null then .
       else (.steps |= map(if . == $target then . + {overlaid_functions:$functions} else . end))
       end' \
    --arg name "$name" --argjson functions "$functions"
}

annotate_verify_scope() {
  local name="$1" scope="$2"
  receipt_update \
    '(.steps | map(select(.name == $name)) | last) as $target
     | if $target == null then .
       else (.steps |= map(if . == $target then . + {scope:$scope} else . end))
       end' \
    --arg name "$name" --arg scope "$scope"
}

set_verdict() {
  local verdict="$1"
  receipt_update \
    '.final_verdict=$verdict | .finished_at=$timestamp' \
    --arg verdict "$verdict" --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

rollback_commands_json() {
  local base route
  base="$(shell_command bash "$APPLY" rollback --env "$ENVJSON" --kit "$KITDIR")"
  if [ "$ROUTE_STARTED" -eq 1 ]; then
    route="$(shell_command bash "$APPLY" rollback-api --env "$ENVJSON" --kit "$KITDIR")"
    jq -n -c --arg base "$base" --arg route "$route" '[$base,$route]'
  else
    jq -n -c --arg base "$base" '[$base]'
  fi
}

print_rollback() {
  local commands
  commands="$(rollback_commands_json)"
  warn "the run stopped after the first target write; rollback was not started"
  printf '%s' "$commands" | jq -r '.[] | "ROLLBACK: " + .' >&2
  receipt_update '.rollback_commands=$commands' --argjson commands "$commands"
}

on_exit() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    if [ -n "$RECEIPT" ] && ! set_verdict "FAILED"; then
      warn "could not mark the receipt FAILED; the receipt is incomplete"
    fi
    if [ "$FINALIZED" -eq 0 ] && [ "$FIRST_WRITE" -eq 1 ]; then
      print_rollback || warn "could not record or print the rollback commands"
    fi
  fi
  cleanup_temps || warn "could not remove one or more temporary files"
}
trap on_exit EXIT

run_step() {
  local name="$1" display="$2" log code timestamp
  shift 2
  CURRENT_STEP="$name"
  log="$(mktemp "${TMPDIR:-/tmp}/autopatch-step.XXXXXX")"
  remember_temp "$log"
  say "$name"
  "$@" > >(tee "$log") 2> >(tee -a "$log" >&2)
  code=$?
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  LAST_LOG="$log"
  record_step "$name" "$display" "$code" "$timestamp" || {
    warn "could not update receipt after $name"
    return 1
  }
  return "$code"
}

run_scoped_verify_step() {
  local name="$1" phase="$2" scope="$3" display code
  display="$(shell_command bash "$APPLY" "$phase" --scope "$scope" --env "$ENVJSON" --kit "$KITDIR")"
  run_step "$name" "$display" \
    bash "$APPLY" "$phase" --scope "$scope" --env "$ENVJSON" --kit "$KITDIR"
  code=$?
  annotate_verify_scope "$name" "$scope" || return 1
  return "$code"
}

stop_on_failure() {
  local name="$1" code="$2"
  warn "$name failed with exit code $code"
  exit "$code"
}

stop_on_verification_failure() {
  local name="$1" code="$2" reason
  if [ "$ROUTES_IN_SCOPE" -eq 1 ]; then
    if [ "$ROUTE_STARTED" -eq 1 ]; then
      reason="withheld because $name failed with exit code $code; rollback-api remains available"
    else
      reason="withheld because $name failed with exit code $code before route apply started"
    fi
    record_skip "finalize-api" \
      "$(shell_command bash "$APPLY" finalize-api --env "$ENVJSON" --kit "$KITDIR")" \
      "$reason" \
      || die "cannot record that finalize-api was withheld"
  fi
  stop_on_failure "$name" "$code"
}

say "preflight (read-only)"
for tool in aws jq python3 curl unzip zip; do
  command -v "$tool" >/dev/null || die "need '$tool' on PATH"
done
if [ -z "$REGION" ]; then
  REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi
if [ -z "$REGION" ]; then
  if configured_region="$(aws configure get region 2>/dev/null)"; then
    REGION="$configured_region"
  fi
fi
[ -n "$REGION" ] || die "region is required; pass --region or configure a default"
IDENTITY="$(aws sts get-caller-identity --region "$REGION" --output json)" \
  || die "caller identity is not readable in region $REGION"
ACCOUNT="$(printf '%s' "$IDENTITY" | jq -r '.Account // empty')"
[ -n "$ACCOUNT" ] || die "caller identity did not include an account"
echo "   account=$ACCOUNT"
echo "   region=$REGION"

record_args=(record "$KITDIR")
if [ -n "$ANSWERS_FILE" ]; then
  record_args+=("$ANSWERS_FILE")
fi
[ ! -f "$ENVJSON" ] || record_args+=(--env "$ENVJSON")
record_command="$(shell_command python3 "$INTERVIEW" "${record_args[@]}")"
if [ -f "$DECISION" ]; then
  python3 "$INTERVIEW" check "$KITDIR" \
    || die "DECISION.json does not match the current manifest; re-run the interview explicitly: $record_command"
else
  say "interview (one-time, before target writes)"
  if [ -z "$ANSWERS_FILE" ] && [ ! -t 0 ]; then
    die "no valid decision and stdin is not a TTY; pass --answers"
  fi
  python3 "$INTERVIEW" "${record_args[@]}" || die "answers were not recorded"
  python3 "$INTERVIEW" check "$KITDIR" || die "decision check failed"
fi

KIT_FINGERPRINT="$(jq -r '.kit_fingerprint // empty' "$DECISION")"
ANSWERS_FINGERPRINT="$(jq -r '.answers_fingerprint // empty' "$DECISION")"
[ -n "$KIT_FINGERPRINT" ] || die "decision has no kit fingerprint"
[ -n "$ANSWERS_FINGERPRINT" ] || die "decision has no answers fingerprint"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RECEIPT="$KITDIR/autopatch-receipt-${STAMP}-$$.json"
jq -n \
  --arg kit "$KIT_FINGERPRINT" --arg answers "$ANSWERS_FINGERPRINT" \
  --arg account "$ACCOUNT" --arg region "$REGION" \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{
    schema_version:1,
    started_at:$timestamp,
    kit_fingerprint:$kit,
    answers_fingerprint:$answers,
    account:$account,
    region:$region,
    steps:[],
    rollback_commands:[],
    final_verdict:"RUNNING"
  }' > "$RECEIPT" || die "cannot initialize receipt"
chmod 0600 "$RECEIPT" || die "cannot protect receipt"
record_step "preflight" "tool and caller-identity checks" 0 \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || die "cannot record preflight"
record_step "interview-check" "$(shell_command python3 "$INTERVIEW" check "$KITDIR")" 0 \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || die "cannot record interview check"

SCOPE="$(jq -r '.answers["environment.scope"] // "control-plane-only"' "$DECISION")"
CONTROL_URL="$(jq -r '.answers["environment.control-plane-url"] // empty' "$DECISION")"
AUTH_CHOICE="$(jq -r '.answers["environment.probe-auth"] // empty' "$DECISION")"
AMI_ID="$(jq -r '.answers["data-plane.ami-id"] // empty' "$DECISION")"
CANARY_ID="$(jq -r '.answers["data-plane.canary-instance-id"] // empty' "$DECISION")"
ALLOW_DRIFT="$(jq -r '.answers["data-plane.allow-base-drift"] // false' "$DECISION")"
case "$SCOPE" in
  control-plane-only)
    DATA_IN_SCOPE=0
    ROUTES_IN_SCOPE=0
    FINAL_VERIFY_SCOPE=control
    ;;
  control-plane-and-data-plane)
    DATA_IN_SCOPE=1
    ROUTES_IN_SCOPE=0
    FINAL_VERIFY_SCOPE=all
    ;;
  control-plane-and-routes)
    DATA_IN_SCOPE=0
    ROUTES_IN_SCOPE=1
    FINAL_VERIFY_SCOPE=control
    ;;
  control-plane-data-plane-and-routes)
    DATA_IN_SCOPE=1
    ROUTES_IN_SCOPE=1
    FINAL_VERIFY_SCOPE=all
    ;;
  *)
    die "unsupported environment.scope: $SCOPE"
    ;;
esac

if ! jq -e '
  [.answers | to_entries[]
   | select(.key | startswith("fix."))
   | select(.key | endswith(".condition-holds"))
   | .value]
  | all
' "$DECISION" >/dev/null; then
  die "a conditional fix was marked out of scope; this aggregate driver cannot safely subtract its artifacts"
fi
if [ "$DATA_IN_SCOPE" -eq 1 ] && ! jq -e '
  [.answers | to_entries[] | select(.key | startswith("manual.data.")) | .value] | all
' "$DECISION" >/dev/null; then
  die "data-plane scope requires every related manual review confirmation"
fi
if [ "$ROUTES_IN_SCOPE" -eq 1 ] && ! jq -e '
  [.answers | to_entries[] | select(.key | startswith("manual.route.")) | .value] | all
' "$DECISION" >/dev/null; then
  die "route scope requires every related manual review confirmation"
fi

URL_INFO="$(python3 - "$CONTROL_URL" <<'PY'
import json
import sys
from urllib.parse import urlparse

parsed = urlparse(sys.argv[1])
host = parsed.hostname or ""
path = parsed.path.strip("/")
parts = path.split("/") if path else []
api_id = ""
stage = ""
labels = host.split(".")
if len(labels) >= 5 and labels[1] == "execute-api":
    api_id = labels[0]
    stage = parts[0] if parts else ""
print(json.dumps({"host": host, "path": path, "api_id": api_id, "stage": stage}))
PY
)" || die "cannot parse control-plane URL"
CONTROL_HOST="$(printf '%s' "$URL_INFO" | jq -r '.host')"
CONTROL_PATH="$(printf '%s' "$URL_INFO" | jq -r '.path')"
CONTROL_API_ID="$(printf '%s' "$URL_INFO" | jq -r '.api_id')"
CONTROL_STAGE="$(printf '%s' "$URL_INFO" | jq -r '.stage')"

resolve_custom_mapping() {
  local mappings selected
  mappings="$(aws apigateway get-base-path-mappings --domain-name "$CONTROL_HOST" \
    --region "$REGION" --output json)" || {
    warn "cannot read custom-domain base-path mappings"
    return 1
  }
  selected="$(printf '%s' "$mappings" | jq -c --arg path "$CONTROL_PATH" '
    [.items[]?
     | .effective=(if .basePath == "(none)" then "" else .basePath end)
     | select(.effective == "" or $path == .effective or ($path | startswith(.effective + "/")))]
    | sort_by(.effective | length)
    | last // empty
  ')" || return 1
  [ -n "$selected" ] || {
    warn "no custom-domain mapping matches the configured URL"
    return 1
  }
  CONTROL_API_ID="$(printf '%s' "$selected" | jq -r '.restApiId // empty')"
  CONTROL_STAGE="$(printf '%s' "$selected" | jq -r '.stage // empty')"
  [ -n "$CONTROL_API_ID" ] && [ -n "$CONTROL_STAGE" ]
}

if [ -z "$CONTROL_API_ID" ]; then
  display="resolve custom-domain API and stage"
  run_step "resolve-api-coordinate" "$display" resolve_custom_mapping
  code=$?
  [ "$code" -eq 0 ] || stop_on_failure "resolve-api-coordinate" "$code"
fi
[ -n "$CONTROL_API_ID" ] || die "control-plane API id could not be resolved"
[ -n "$CONTROL_STAGE" ] || die "control-plane stage could not be resolved"

PROBE_HEADERS_FILE=""
resolve_probe_headers() {
  local plans plan_ids plan_count plan_id keys key_count key_value
  plans="$(aws apigateway get-usage-plans --region "$REGION" --output json)" || {
    warn "cannot list usage plans"
    return 1
  }
  plan_ids="$(printf '%s' "$plans" | jq -r \
    --arg api "$CONTROL_API_ID" --arg stage "$CONTROL_STAGE" '
      .items[]?
      | select(any(.apiStages[]?; .apiId == $api and .stage == $stage))
      | .id
    ')"
  plan_count="$(printf '%s\n' "$plan_ids" | awk 'NF {count++} END {print count+0}')"
  [ "$plan_count" -eq 1 ] || {
    warn "expected one usage plan for the resolved API and stage; found $plan_count"
    return 1
  }
  plan_id="$(printf '%s\n' "$plan_ids" | awk 'NF {print; exit}')"
  keys="$(aws apigateway get-usage-plan-keys --usage-plan-id "$plan_id" \
    --include-values --region "$REGION" --output json)" || {
    warn "cannot read usage-plan keys"
    return 1
  }
  key_count="$(printf '%s' "$keys" | jq '[.items[]? | select(.type == "API_KEY" and (.value // "") != "")] | length')"
  [ "$key_count" -eq 1 ] || {
    warn "expected one API key in the resolved usage plan; found $key_count"
    return 1
  }
  key_value="$(printf '%s' "$keys" | jq -r '.items[] | select(.type == "API_KEY" and (.value // "") != "") | .value')"
  # Discovery requires a headers file. Keep the key only in this mode-0600
  # temporary file, remove it on exit, and never put it in DECISION.json, the
  # receipt, or a log line.
  PROBE_HEADERS_FILE="$(mktemp "${TMPDIR:-/tmp}/autopatch-headers.XXXXXX.json")"
  remember_temp "$PROBE_HEADERS_FILE"
  chmod 0600 "$PROBE_HEADERS_FILE" || return 1
  jq -n --arg value "$key_value" '{"x-api-key":$value}' > "$PROBE_HEADERS_FILE" || return 1
  key_value=""
}

if [ "$AUTH_CHOICE" = "resolve-from-usage-plan" ]; then
  echo "   API key value: mode-0600 temporary headers file only; removed on exit; not written to DECISION.json, the receipt, or logs"
  run_step "resolve-probe-auth" "resolve probe headers from the API usage plan" resolve_probe_headers
  code=$?
  [ "$code" -eq 0 ] || stop_on_failure "resolve-probe-auth" "$code"
else
  PROBE_HEADERS_FILE="$AUTH_CHOICE"
fi
[ -f "$PROBE_HEADERS_FILE" ] || die "probe headers file not found"

discover_target() {
  local output_path tmp
  OC_CONTROL_PLANE_URL="$CONTROL_URL" \
  OC_CONTROL_PLANE_API_ID="$CONTROL_API_ID" \
  OC_CONTROL_PLANE_PROBE_HEADERS_FILE="$PROBE_HEADERS_FILE" \
    bash "$DISCOVER" "$REGION" "$MANIFEST" || return $?
  output_path="$DEFAULT_ENV"
  [ -f "$output_path" ] || {
    warn "discovery did not write $output_path"
    return 1
  }
  if [ "$ENVJSON" != "$output_path" ]; then
    cp "$output_path" "$ENVJSON" || return 1
  fi
  tmp="$(mktemp "${TMPDIR:-/tmp}/autopatch-env.XXXXXX")"
  remember_temp "$tmp"
  jq --arg ami "$AMI_ID" --arg canary "$CANARY_ID" '
    if $ami == "" then . else .new_ami_id=$ami end
    | if $canary == "" then . else .canary_instance_id=$canary end
  ' "$ENVJSON" > "$tmp" || return 1
  mv "$tmp" "$ENVJSON"
}

run_step "discover" "$(shell_command bash "$DISCOVER" "$REGION" "$MANIFEST")" discover_target
code=$?
[ "$code" -eq 0 ] || stop_on_failure "discover" "$code"

require_confirmed() {
  local label="$1" expression="$2"
  if ! jq -e "$expression" "$ENVJSON" >/dev/null; then
    warn "unconfirmed coordinate: $label"
    return 1
  fi
}
require_confirmed "control-plane API" '.control_plane_api.confirmed == true' \
  || stop_on_failure "discover gate" 1
require_confirmed "API-package peer discovery" '.lambda_link.peer_discovery_confirmed == true' \
  || stop_on_failure "discover gate" 1
if [ "$DATA_IN_SCOPE" -eq 1 ]; then
  require_confirmed "host ASG" '.asg.confirmed == true and (.asg.name // "") != ""' \
    || stop_on_failure "discover gate" 1
  require_confirmed "assets bucket" '.assets.confirmed == true and (.assets.bucket // "") != ""' \
    || stop_on_failure "discover gate" 1
fi

OVERLAY_FUNCTIONS="$(jq -c '
  [
    .lambda_link.function,
    (.lambda_link.peers[]? | select(.probe_paths_present == true) | .function)
  ]
  | map(select(. != null and . != ""))
  | unique
' "$ENVJSON")" || die "cannot derive the overlay function list"

PRECHECK_DISPLAY="$(shell_command bash "$APPLY" precheck --env "$ENVJSON" --kit "$KITDIR")"
run_step "precheck" "$PRECHECK_DISPLAY" \
  bash "$APPLY" precheck --env "$ENVJSON" --kit "$KITDIR"
code=$?
[ "$code" -eq 0 ] || stop_on_failure "precheck" "$code"

read_precheck_state() {
  python3 - "$LAST_LOG" "$1" <<'PY'
import re
import sys

pattern = re.compile(r"STATE\s+" + re.escape(sys.argv[2]) + r"=(ALREADY|READY|DRIFT|BLOCKED)")
state = ""
with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
    for line in handle:
        match = pattern.search(line)
        if match:
            state = match.group(1)
print(state)
PY
}
HOOK_STATE="$(read_precheck_state hook)"
BOOTSTRAP_STATE="$(read_precheck_state bootstrap)"
OVERLAY_STATE="$(read_precheck_state overlay)"
[ -n "$HOOK_STATE" ] || die "precheck did not report the hook state"
[ -n "$BOOTSTRAP_STATE" ] || die "precheck did not report the bootstrap state"
[ -n "$OVERLAY_STATE" ] || die "precheck did not report the overlay state"
[ "$OVERLAY_STATE" != "BLOCKED" ] || die "precheck blocked the overlay scope"
if [ "$DATA_IN_SCOPE" -eq 1 ] && [ "$BOOTSTRAP_STATE" = "DRIFT" ] \
    && [ "$ALLOW_DRIFT" != "true" ]; then
  die "bootstrap DRIFT was not approved during the interview"
fi

DATA_ALREADY=0
[ "$HOOK_STATE" = "ALREADY" ] && [ "$BOOTSTRAP_STATE" = "ALREADY" ] \
  && DATA_ALREADY=1
OVERLAY_ALREADY=0
[ "$OVERLAY_STATE" = "ALREADY" ] && OVERLAY_ALREADY=1

say "recorded plan"
echo "   scope=$SCOPE"
echo "   overlay_functions=$(printf '%s' "$OVERLAY_FUNCTIONS" | jq -c '.')"
echo "   kit_fingerprint=$KIT_FINGERPRINT"
echo "   answers_fingerprint=$ANSWERS_FINGERPRINT"
PHASES=()
NEEDS_WRITE=0
if [ "$DATA_IN_SCOPE" -eq 1 ]; then
  if [ "$DATA_ALREADY" -eq 0 ] || [ "$OVERLAY_ALREADY" -eq 0 ]; then
    PHASES+=(backup apply)
    NEEDS_WRITE=1
  fi
  if [ "$DATA_ALREADY" -eq 0 ]; then
    PHASES+=(canary verify refresh)
  fi
elif [ "$OVERLAY_ALREADY" -eq 0 ]; then
  PHASES+=(backup apply-control)
  NEEDS_WRITE=1
fi
if [ "$ROUTES_IN_SCOPE" -eq 1 ]; then
  [ "$NEEDS_WRITE" -eq 1 ] || PHASES+=(backup)
  PHASES+=(apply-api verify-api)
  NEEDS_WRITE=1
fi
PHASES+=(verify)
[ "$ROUTES_IN_SCOPE" -eq 0 ] || PHASES+=(verify-api finalize-api)
printf '   phases='
printf '%s ' "${PHASES[@]}"
printf '\n'

if [ "$ASSUME_YES" -ne 1 ]; then
  [ -t 0 ] || die "non-interactive execution requires --yes after the recorded plan"
  printf "Proceed with this recorded plan? [y/N] "
  read -r confirmation
  case "$confirmation" in y|Y|yes|YES) ;; *) die "recorded plan was not confirmed" ;; esac
fi

if [ "$NEEDS_WRITE" -eq 1 ]; then
  FIRST_WRITE=1
  backup_display="$(shell_command bash "$APPLY" backup --env "$ENVJSON" --kit "$KITDIR")"
  run_step "backup" "$backup_display" \
    bash "$APPLY" backup --env "$ENVJSON" --kit "$KITDIR"
  code=$?
  [ "$code" -eq 0 ] || stop_on_failure "backup" "$code"
else
  record_skip "backup" "$(shell_command bash "$APPLY" backup --env "$ENVJSON" --kit "$KITDIR")" \
    "every selected apply concern was already in service" \
    || die "cannot record skipped backup"
fi

if [ "$DATA_IN_SCOPE" -eq 1 ]; then
  if [ "$DATA_ALREADY" -eq 1 ] && [ "$OVERLAY_ALREADY" -eq 1 ]; then
    record_skip "apply" "$(shell_command bash "$APPLY" apply --env "$ENVJSON" --kit "$KITDIR")" \
      "hook, bootstrap, and overlay were ALREADY" \
      || die "cannot record skipped apply"
  else
    apply_args=(apply --env "$ENVJSON" --kit "$KITDIR")
    [ "$ALLOW_DRIFT" != "true" ] || apply_args+=(--allow-base-drift)
    run_step "apply" "$(shell_command bash "$APPLY" "${apply_args[@]}")" \
      bash "$APPLY" "${apply_args[@]}"
    code=$?
    [ "$code" -eq 0 ] || stop_on_failure "apply" "$code"
    [ "$OVERLAY_ALREADY" -eq 1 ] \
      || annotate_overlay "apply" "$OVERLAY_FUNCTIONS" \
      || die "cannot record the apply overlay scope"
  fi
  if [ "$DATA_ALREADY" -eq 0 ]; then
    run_step "canary" "$(shell_command bash "$APPLY" canary --env "$ENVJSON" --kit "$KITDIR")" \
      bash "$APPLY" canary --env "$ENVJSON" --kit "$KITDIR"
    code=$?
    [ "$code" -eq 0 ] || stop_on_failure "canary" "$code"
    if [ -n "$CANARY_ID" ] && [ "$CANARY_ID" != "auto" ]; then
      observed_canary="$(jq -r '.canary_instance_id // empty' "$KITDIR/.restorepatch-state.json")"
      [ "$observed_canary" = "$CANARY_ID" ] \
        || die "canary phase selected $observed_canary, not the recorded instance $CANARY_ID"
    fi
    run_scoped_verify_step "verify-before-refresh" verify data
    code=$?
    [ "$code" -eq 0 ] || stop_on_verification_failure "verify-before-refresh" "$code"
    run_step "refresh" "$(shell_command bash "$APPLY" refresh --env "$ENVJSON" --kit "$KITDIR")" \
      bash "$APPLY" refresh --env "$ENVJSON" --kit "$KITDIR"
    code=$?
    [ "$code" -eq 0 ] || stop_on_failure "refresh" "$code"
  else
    record_skip "canary" "$(shell_command bash "$APPLY" canary --env "$ENVJSON" --kit "$KITDIR")" \
      "data-plane concerns were ALREADY" \
      || die "cannot record skipped canary"
    record_skip "refresh" "$(shell_command bash "$APPLY" refresh --env "$ENVJSON" --kit "$KITDIR")" \
      "data-plane concerns were ALREADY" \
      || die "cannot record skipped refresh"
  fi
else
  if [ "$OVERLAY_ALREADY" -eq 1 ]; then
    record_skip "apply-control" \
      "$(shell_command bash "$APPLY" apply-control --env "$ENVJSON" --kit "$KITDIR")" \
      "overlay was ALREADY" \
      || die "cannot record skipped apply-control"
  else
    run_step "apply-control" \
      "$(shell_command bash "$APPLY" apply-control --env "$ENVJSON" --kit "$KITDIR")" \
      bash "$APPLY" apply-control --env "$ENVJSON" --kit "$KITDIR"
    code=$?
    [ "$code" -eq 0 ] || stop_on_failure "apply-control" "$code"
    annotate_overlay "apply-control" "$OVERLAY_FUNCTIONS" \
      || die "cannot record the apply-control overlay scope"
  fi
fi

if [ "$ROUTES_IN_SCOPE" -eq 1 ]; then
  ROUTE_STARTED=1
  run_step "apply-api" "$(shell_command bash "$APPLY" apply-api --env "$ENVJSON" --kit "$KITDIR")" \
    bash "$APPLY" apply-api --env "$ENVJSON" --kit "$KITDIR"
  code=$?
  [ "$code" -eq 0 ] || stop_on_failure "apply-api" "$code"
  run_scoped_verify_step "verify-api" verify-api routes
  code=$?
  [ "$code" -eq 0 ] || stop_on_verification_failure "verify-api" "$code"
fi

run_scoped_verify_step "final-verify" verify "$FINAL_VERIFY_SCOPE"
code=$?
[ "$code" -eq 0 ] || stop_on_verification_failure "final-verify" "$code"
if [ "$ROUTES_IN_SCOPE" -eq 1 ]; then
  run_scoped_verify_step "final-verify-api" verify-api routes
  code=$?
  [ "$code" -eq 0 ] || stop_on_verification_failure "final-verify-api" "$code"
  # Starting finalization can destroy the route rollback window even if the
  # finalization command or its receipt update later fails.
  FINALIZED=1
  run_step "finalize-api" "$(shell_command bash "$APPLY" finalize-api --env "$ENVJSON" --kit "$KITDIR")" \
    bash "$APPLY" finalize-api --env "$ENVJSON" --kit "$KITDIR"
  code=$?
  [ "$code" -eq 0 ] || stop_on_failure "finalize-api" "$code"
fi

set_verdict "PASS" || die "cannot finalize receipt"
FINALIZED=1
say "unattended patch PASS"
echo "receipt: $RECEIPT"
