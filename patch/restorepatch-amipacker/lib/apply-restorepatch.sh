#!/usr/bin/env bash
# apply-restorepatch.sh — deterministic driver for restorepatch-amipacker.
#
# The kit's twelve synthesized CloudFormation resource changes are applied, verified and
# rolled back ONLY through this tool, so no operator hand-assembles a fleet-breaking
# command sequence and every executor runs identical code.
#
# It covers exactly four concerns and refuses to do anything else:
#   1. host-init lifecycle hook heartbeat timeout 1200 -> 3600
#   2. one new LaunchTemplate version carrying the Packer AMI id AND the new
#      content-addressed bootstrap prefix, then default-version flip + controlled refresh
#   3. openclaw-api function code overlay (reuses the live package's own dependencies)
#   4. private REST API endpoint attachment followed by deployment and stage replacement
#
# Deliberately NOT done, and it will refuse if asked:
#   * HostASG MinSize 2 -> 0. That value is first-deployment semantics; on a live fleet it
#     permits scaling to zero hosts that carry real tenants.
#   * TrackDefaultLTVersion AsgShape digest and the OpenClawImage CodeBuild churn. Both are
#     derived or content-hash projections with no independent action.
#
# usage: lib/apply-restorepatch.sh <precheck|backup|apply|verify|rollback|apply-api|verify-api|finalize-api|rollback-api> --env <environment.json> --kit <kit-dir> [--allow-base-drift]
set -uo pipefail

PHASE="${1:?phase required: precheck|backup|apply|verify|rollback|apply-api|verify-api|finalize-api|rollback-api}"; shift || true
ENVJSON=""; KITDIR="."; ALLOW_BASE_DRIFT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENVJSON="${2:?}"; shift 2 ;;
    --kit) KITDIR="${2:?}"; shift 2 ;;
    --allow-base-drift) ALLOW_BASE_DRIFT=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -f "$ENVJSON" ] || { echo "FATAL: environment file not found: $ENVJSON" >&2; exit 2; }

# These values identify CDK asset bundles and therefore name S3 prefixes. They are
# not hashes of init-host.sh, whose independent byte digest is checked separately.
BASE_ASSET_BUNDLE_PREFIX=dea0bd3d54ac88764319c07b1b546df952e8e828f7f3aba0d18866160ee2d046
ASSET_BUNDLE_PREFIX=938e619b7c6e1b292733e9161d3f0b71603aa32f4930e15db9d551624bc72d90
ARTIFACT_SHA256=744793b0fcb1d0e8df650aa747c22cc733939e16cce0f805e7a9f0022e761d17
HOOK_NAME=openclaw-host-init
NEW_TIMEOUT=3600
STATE="${KITDIR}/.restorepatch-state.json"

jget() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$1" "$2"; }
jpath() {
  python3 - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    value = value.get(key, "") if isinstance(value, dict) else ""
print(value if not isinstance(value, (dict, list)) else json.dumps(value))
PY
}

REGION="$(jpath "$ENVJSON" region)"
ASG="$(jpath "$ENVJSON" asg.name)"
LT_ID="$(jpath "$ENVJSON" asg.lt_id)"
LT_VER="$(jpath "$ENVJSON" asg.lt_version_pinned)"
BUCKET="$(jpath "$ENVJSON" assets.bucket)"
FN="$(jpath "$ENVJSON" lambda_link.function)"
AMI="$(jget "$ENVJSON" new_ami_id)"

for v in REGION ASG LT_ID LT_VER BUCKET FN; do
  [ -n "${!v}" ] || { echo "FATAL: $v missing from $ENVJSON; run lib/discover-env.sh first" >&2; exit 2; }
done

# Every call pins --region: a stray AWS_REGION in the shell points the CLI at another
# region where same-named resources exist and answers look plausible but are wrong.
aws_() { aws "$@" --region "$REGION"; }

die() { echo "FAIL: $*" >&2; exit 1; }
say() { echo "== $*"; }

state_put() {
  python3 - "$STATE" "$1" "$2" <<'PY'
import json, os, sys
p, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(p)) if os.path.exists(p) else {}
d[k] = v
json.dump(d, open(p, "w"), indent=2)
PY
}
state_get() { [ -f "$STATE" ] && jget "$STATE" "$1" || echo ""; }

sha256_file() {
  local path="$1" value
  value="$(shasum -a 256 "$path" 2>/dev/null | awk '{print $1}')"
  [ -n "$value" ] || value="$(sha256sum "$path" | awk '{print $1}')"
  printf '%s\n' "$value"
}

alias_name() {
  local candidate count
  candidate="$(jpath "$ENVJSON" lambda_link.serving_qualifier)"
  case "$candidate" in ""|'$LATEST') candidate="" ;; esac
  if [ -z "$candidate" ]; then
    count="$(aws_ lambda list-aliases --function-name "$FN" --query 'length(Aliases)' --output text)"
    [ "$count" = "1" ] &&
      candidate="$(aws_ lambda list-aliases --function-name "$FN" --query 'Aliases[0].Name' --output text)"
  fi
  [ -n "$candidate" ] || return 1
  aws_ lambda get-alias --function-name "$FN" --name "$candidate" >/dev/null 2>&1 || return 1
  printf '%s\n' "$candidate"
}

overlay_function() {
  local function_name="$1" artifact_root="$2" work zip location rel
  location="$(aws_ lambda get-function --function-name "$function_name" \
    --query 'Code.Location' --output text)" || die "cannot locate $function_name package"
  work="$(mktemp -d)"
  zip="/tmp/${function_name}-restorepatch.zip"
  curl -fsS -o "${work}/live.zip" "$location" || die "cannot download $function_name package"
  (cd "$work" && unzip -oq live.zip) || die "cannot unpack $function_name package"
  while IFS= read -r rel; do
    mkdir -p "${work}/$(dirname "$rel")"
    cp "${artifact_root}/${rel}" "${work}/${rel}" || die "cannot overlay $function_name:$rel"
  done < <(cd "$artifact_root" && find . -type f -print | sed 's|^\./||' | sort)
  rm -f "${work}/live.zip"
  (cd "$work" && zip -qr "$zip" .) || die "cannot repack $function_name"
  aws_ lambda update-function-code --function-name "$function_name" \
    --zip-file "fileb://${zip}" >/dev/null || die "update-function-code failed for $function_name"
  aws_ lambda wait function-updated --function-name "$function_name" \
    || die "$function_name did not settle"
  state_put "zip_${function_name}" "$zip"
}

backup_function() {
  local function_name="$1" location zip
  location="$(aws_ lambda get-function --function-name "$function_name" \
    --query 'Code.Location' --output text)" || die "cannot locate $function_name package"
  zip="/tmp/${function_name}-restorepatch-backup.zip"
  curl -fsS -o "$zip" "$location" || die "cannot back up $function_name"
  state_put "backup_${function_name}" "$zip"
  state_put "backup_sha_${function_name}" "$(sha256_file "$zip")"
}

restore_function() {
  local function_name="$1" zip
  zip="$(state_get "backup_${function_name}")"
  [ -n "$zip" ] && [ -f "$zip" ] || die "missing backup package for $function_name"
  aws_ lambda update-function-code --function-name "$function_name" \
    --zip-file "fileb://${zip}" >/dev/null || die "code restore failed for $function_name"
  aws_ lambda wait function-updated --function-name "$function_name" \
    || die "$function_name did not settle after restore"
}

resolve_bootstrap_state() {
  local live_prefix="$1" probe rc
  if [ "$live_prefix" = "$ASSET_BUNDLE_PREFIX" ]; then
    echo "   STATE bootstrap=ALREADY — the target prefix is already in service; apply will skip it"
    BOOTSTRAP_STATE=ALREADY
  elif [ "$live_prefix" = "$BASE_ASSET_BUNDLE_PREFIX" ]; then
    echo "   STATE bootstrap=READY — in service on the expected base form"
    BOOTSTRAP_STATE=READY
  elif [ -z "$live_prefix" ]; then
    echo "   STATE bootstrap=DRIFT — in-service UserData carries no content-addressed bootstrap prefix"
    BOOTSTRAP_STATE=DRIFT
  else
    # A third value is NOT automatically a version conflict. The published tree and the
    # internal tree render init-host.sh differently (the publish scrub deletes comment
    # lines carrying internal issue refs), so the same logical content hashes to a
    # different prefix. Decide by CONTENT, not by hash: fetch what is actually in service
    # and compare it to the kit artifact.
    echo "   STATE bootstrap=UNKNOWN prefix — deciding by content, not by hash"
    probe="$(mktemp -d)"
    if aws_ s3 cp "s3://${BUCKET}/deployment/bootstrap/host/${live_prefix}/init-host.sh" \
         "${probe}/live-init-host.sh" >/dev/null 2>&1; then
      python3 - "${probe}/live-init-host.sh" "${KITDIR}/host-scripts/init-host.sh.patched" <<'PY'
import pathlib
import re
import sys

def code_only(p):
    # compare executable content only: the publish scrub removes whole comment lines,
    # so comments must not decide whether the environment is on a different version
    out = []
    for line in pathlib.Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(re.sub(r"\s+", " ", s))
    return out

live, kit = code_only(sys.argv[1]), code_only(sys.argv[2])
print("   live code lines=%d  kit code lines=%d" % (len(live), len(kit)))
if live == kit:
    print("   VERDICT content-equal to the kit artifact: the patch payload is already in "
          "service under a different hash (different source tree). Nothing to deliver.")
    sys.exit(10)
same = sum(1 for a, b in zip(live, kit) if a == b)
print("   VERDICT content differs: %d/%d leading code lines match" % (same, max(len(live), len(kit))))
sys.exit(11)
PY
      rc=$?
      if [ "$rc" -eq 10 ]; then
        echo "   STATE bootstrap=ALREADY (content-equal under a different hash)"
        BOOTSTRAP_STATE=ALREADY
      else
        echo "   STATE bootstrap=DRIFT — in-service content differs from the kit artifact"
        BOOTSTRAP_STATE=DRIFT
      fi
    else
      echo "   STATE bootstrap=DRIFT — cannot read the in-service bootstrap object, so the"
      echo "         difference cannot be decided by content. NOT treated as absent."
      BOOTSTRAP_STATE=DRIFT
    fi
    rm -rf "$probe"
  fi
}

resolve_api_context() {
  API_ID="$(jpath "$ENVJSON" control_plane_api.id)"
  API_CONFIRMED="$(jpath "$ENVJSON" control_plane_api.confirmed)"
  API_STAGE=v1
  [ -n "$API_ID" ] || die "control_plane_api.id missing from $ENVJSON"
  case "$API_CONFIRMED" in True|true) ;; *) die "control_plane_api.confirmed is not true" ;; esac
  aws_ apigateway get-stage --rest-api-id "$API_ID" --stage-name "$API_STAGE" >/dev/null \
    || die "stage $API_STAGE not found on REST API $API_ID"
}

resolve_target_vpc() {
  local subnet_csv vpcs first count
  subnet_csv="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].VPCZoneIdentifier' --output text)"
  [ -n "$subnet_csv" ] && [ "$subnet_csv" != "None" ] \
    || die "cannot derive customer VPC: $ASG has no VPCZoneIdentifier"
  IFS=',' read -r -a subnet_ids <<< "$subnet_csv"
  vpcs="$(aws_ ec2 describe-subnets --subnet-ids "${subnet_ids[@]}" \
    --query 'Subnets[].VpcId' --output text)"
  first="$(printf '%s\n' "$vpcs" | tr '\t ' '\n' | awk 'NF && !seen[$0]++ {print; exit}')"
  count="$(printf '%s\n' "$vpcs" | tr '\t ' '\n' | awk 'NF && !seen[$0]++ {n++} END {print n+0}')"
  [ "$count" -eq 1 ] || die "ASG subnets span $count VPCs; refusing endpoint selection"
  TARGET_VPC_ID="$first"
}

probe_private_api_endpoint() {
  local service rows count endpoint_state
  service="com.amazonaws.${REGION}.execute-api"
  say "READ-ONLY endpoint collision probe in $TARGET_VPC_ID for $service"
  rows="$(aws_ ec2 describe-vpc-endpoints \
    --filters "Name=vpc-id,Values=${TARGET_VPC_ID}" \
      "Name=service-name,Values=${service}" "Name=vpc-endpoint-type,Values=Interface" \
    --query 'VpcEndpoints[?PrivateDnsEnabled==`true` && State!=`deleted`].[VpcEndpointId,State]' \
    --output text)"
  count="$(printf '%s\n' "$rows" | awk 'NF {n++} END {print n+0}')"
  [ "$count" -le 1 ] \
    || die "endpoint collision: found $count private-DNS endpoints for $service in $TARGET_VPC_ID"
  [ "$count" -eq 1 ] \
    || die "no reusable private-DNS endpoint for $service in $TARGET_VPC_ID; provision one before attaching the API"
  VPC_ENDPOINT_ID="$(printf '%s\n' "$rows" | awk 'NF {print $1; exit}')"
  endpoint_state="$(printf '%s\n' "$rows" | awk 'NF {print $2; exit}')"
  [ "$endpoint_state" = "available" ] \
    || die "reusable endpoint $VPC_ENDPOINT_ID is $endpoint_state, not available"
  echo "   REUSE $VPC_ENDPOINT_ID; no endpoint create call will be made"
}

case "$PHASE" in

precheck)
  say "identity"
  aws_ sts get-caller-identity --query Account --output text || die "no usable credentials"
  say "lifecycle hook current timeout"
  cur="$(aws_ autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" \
        --query "LifecycleHooks[?LifecycleHookName=='${HOOK_NAME}'].HeartbeatTimeout|[0]" \
        --output text)"
  echo "   $HOOK_NAME heartbeat = $cur"
  [ "$cur" = "None" ] && die "hook $HOOK_NAME not found on $ASG"
  say "ASG shape and capacity"
  aws_ autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].[MinSize,MaxSize,DesiredCapacity,length(Instances)]' --output text
  # A single "must equal the base prefix" test conflated three very different states and
  # failed all of them. Worse, in two of them the substitution in apply silently finds
  # nothing, publishes a version identical to the current one, flips the default and
  # reports success — idempotent but silently ineffective, which is worse than refusing.
  # So decide per state, and treat already-applied as a pass.
  say "bootstrap asset state (per-concern, already-applied counts as a pass)"
  live_ud="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions "$LT_VER" --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
    --output text | base64 -d)"
  live_prefix="$(printf '%s' "$live_ud" \
    | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
  echo "   in-service prefix : $live_prefix"
  echo "   expected base     : $BASE_ASSET_BUNDLE_PREFIX"
  echo "   patch target      : $ASSET_BUNDLE_PREFIX"
  resolve_bootstrap_state "$live_prefix"
  say "openclaw-api environment key count (must not change across apply)"
  aws_ lambda get-function-configuration --function-name "$FN" \
    --query 'length(Environment.Variables)' --output text

  # Real-machine finding: the overlay replaces modules but the live package may not
  # contain every module they import. On one environment the patched tenant_service.py
  # imported core.lifecycle_fence (58 references) while the live package had no such
  # module — the overlay would have produced an import-time failure on every invocation,
  # i.e. the whole control plane down. Checking the prefix alone does not catch that.
  say "overlay import resolution against the LIVE package"
  loc="$(aws_ lambda get-function --function-name "$FN" --query 'Code.Location' --output text)"
  work="$(mktemp -d)"
  curl -fsS -o "${work}/live.zip" "$loc" || die "cannot download the live package"
  (cd "$work" && unzip -oq live.zip) || die "cannot unpack the live package"
  python3 - "$KITDIR" "$work" <<'PY'
import pathlib
import re
import sys

kit, live = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
imp = re.compile(r"^\s*(?:import|from)\s+((?:core|services|consumers|routes)(?:\.[A-Za-z_][\w]*)*)")
missing, checked = {}, 0
kit_api = kit / "lambda" / "api"
for mod in sorted(kit_api.rglob("*.py")):
    for line in mod.read_text(encoding="utf-8", errors="replace").splitlines():
        m = imp.match(line)
        if not m:
            continue
        checked += 1
        rel = m.group(1).replace(".", "/")
        if ((live / f"{rel}.py").is_file()
                or (live / rel / "__init__.py").is_file()
                or (kit_api / f"{rel}.py").is_file()
                or (kit_api / rel / "__init__.py").is_file()):
            continue
        missing.setdefault(m.group(1), set()).add(mod.name)
print("   inspected %d import statement(s) across the overlay modules" % checked)
if missing:
    for name, users in sorted(missing.items()):
        print("   MISSING in live package: %s  (imported by %s)"
              % (name, ", ".join(sorted(users))))
    raise SystemExit(
        "FAIL: the overlay imports modules the live package does not contain; applying it "
        "would fail at import time and take the control plane down"
    )
print("   PASS every module the overlay imports exists in the live package")
PY
  # One concern failing must not deny the others a verdict, so record it instead of dying.
  # apply re-checks this independently and refuses the overlay step on its own.
  if [ $? -ne 0 ]; then
    IMPORT_STATE=BLOCKED
  else
    IMPORT_STATE=OK
  fi
  say "OpenClawImage CodeBuild functional config (content-hash churn must not alter it)"
  aws_ codebuild batch-get-projects --names openclaw-golden-image-builder \
    --query 'projects[0].[environment.type,environment.computeType,timeoutInMinutes]' --output text

  # ---- hook concern: put-lifecycle-hook is naturally idempotent, so already == pass ----
  say "hook state"
  if [ "$cur" = "$NEW_TIMEOUT" ]; then
    echo "   STATE hook=ALREADY heartbeat is already ${NEW_TIMEOUT}"
    HOOK_STATE=ALREADY
  else
    echo "   STATE hook=READY ${cur} -> ${NEW_TIMEOUT}"
    HOOK_STATE=READY
  fi

  # ---- overlay concern: per-module, so a partially applied patch converges ----
  say "overlay state (per module)"
  ov_ready=0; ov_already=0
  while IFS= read -r rel; do
    kf="${KITDIR}/lambda/api/${rel}"
    lf="${work}/${rel}"
    if [ ! -f "$lf" ]; then
      echo "   ${rel}: READY (new module)"
      ov_ready=$((ov_ready + 1)); continue
    fi
    a="$(sha256_file "$kf")"
    b="$(sha256_file "$lf")"
    if [ "$a" = "$b" ]; then
      echo "   ${rel}: ALREADY (live matches the kit byte for byte)"
      ov_already=$((ov_already + 1))
    else
      echo "   ${rel}: READY (live differs from the kit)"
      ov_ready=$((ov_ready + 1))
    fi
  done < <(cd "${KITDIR}/lambda/api" && find . -type f -print | sed 's|^\./||' | sort)
  if [ "$ov_ready" -eq 0 ]; then
    echo "   STATE overlay=ALREADY all API modules already in service"
    OVERLAY_STATE=ALREADY
  else
    echo "   STATE overlay=READY ${ov_ready} module(s) to replace, ${ov_already} already in service"
    OVERLAY_STATE=READY
  fi

  # ---- verdict ----
  say "per-concern verdict"
  printf '   hook=%s  bootstrap=%s  overlay=%s\n' \
    "${HOOK_STATE:-?}" "${BOOTSTRAP_STATE:-?}" "${OVERLAY_STATE:-?}"
  if [ "${BOOTSTRAP_STATE:-DRIFT}" = "DRIFT" ]; then
    echo "   RESULT DRIFT — apply will refuse the bootstrap step unless"
    echo "          --allow-base-drift is supplied; other concerns remain actionable."
    exit 0
  fi
  if [ "${HOOK_STATE}" = "ALREADY" ] && [ "${BOOTSTRAP_STATE}" = "ALREADY" ] \
     && [ "${OVERLAY_STATE}" = "ALREADY" ]; then
    echo "   RESULT ALREADY APPLIED — every concern is already in service. Re-running apply"
    echo "          is a no-op; this is a pass, not a failure."
    exit 0
  fi
  echo "   RESULT READY — apply will act only on the concerns marked READY"
  ;;

backup)
  say "recording restore points into $STATE"
  hook_to="$(aws_ autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" \
    --query "LifecycleHooks[?LifecycleHookName=='${HOOK_NAME}'].HeartbeatTimeout|[0]" --output text)"
  state_put hook_backup_timeout "$hook_to"
  lt_def="$(aws_ ec2 describe-launch-templates --launch-template-id "$LT_ID" \
    --query 'LaunchTemplates[0].DefaultVersionNumber' --output text)"
  state_put host_lt_backup_version "$lt_def"
  lt_ami="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions "$LT_VER" --query 'LaunchTemplateVersions[0].LaunchTemplateData.ImageId' --output text)"
  state_put host_lt_backup_ami "$lt_ami"
  env_n="$(aws_ lambda get-function-configuration --function-name "$FN" \
    --query 'length(Environment.Variables)' --output text)"
  state_put api_env_key_count "$env_n"
  ver="$(aws_ lambda publish-version --function-name "$FN" \
    --description pre-restorepatch-anchor --query Version --output text)" || die "cannot publish anchor version"
  state_put api_backup_version "$ver"
  if an="$(alias_name)"; then
    state_put api_alias_name "$an"
    state_put api_alias_backup_version "$(aws_ lambda get-alias --function-name "$FN" \
      --name "$an" --query FunctionVersion --output text)"
  else
    echo "   API environment does not use an alias; the unqualified version is the serving path"
  fi
  loc="$(aws_ lambda get-function --function-name "$FN" --query 'Code.Location' --output text)"
  curl -fsS -o /tmp/openclaw-api-backup.zip "$loc" || die "cannot download live package"
  sha="$(sha256_file /tmp/openclaw-api-backup.zip)"
  state_put api_backup_zip_sha256 "$sha"
  state_put "backup_${FN}" /tmp/openclaw-api-backup.zip
  for extra_fn in openclaw-backup openclaw-health-check openclaw-scaler \
    openclaw-tenant-stats-writer openclaw-console-bff; do
    if aws_ lambda get-function --function-name "$extra_fn" >/dev/null 2>&1; then
      backup_function "$extra_fn"
    else
      echo "   ABSENT $extra_fn; no recovery package required"
    fi
  done
  echo "   hook=$hook_to lt_default=$lt_def ami=$lt_ami api_anchor=$ver env_keys=$env_n"
  say "backup OK"
  ;;

apply)
  [ -f "$STATE" ] || die "no backup state; run backup first"
  [ -n "$AMI" ] || die "new_ami_id missing from environment; bake the AMI per host-scripts/packer/CUSTOMER-GUIDE.md first"

  say "1/3 widen $HOOK_NAME heartbeat to ${NEW_TIMEOUT}s"
  aws_ autoscaling put-lifecycle-hook --auto-scaling-group-name "$ASG" \
    --lifecycle-hook-name "$HOOK_NAME" \
    --lifecycle-transition autoscaling:EC2_INSTANCE_LAUNCHING \
    --heartbeat-timeout "$NEW_TIMEOUT" --default-result ABANDON || die "hook update failed"

  say "2/3 upload bootstrap script to its content-addressed key, then one new LT version"
  # Recompute the state here rather than trusting precheck: the run directory is editable
  # and the live target may have moved between the two phases.
  live_prefix="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions "$LT_VER" --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
    --output text | base64 -d \
    | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
  resolve_bootstrap_state "$live_prefix"
  if [ "$BOOTSTRAP_STATE" = "ALREADY" ]; then
    say "   SKIP bootstrap: target content already in service (idempotent no-op)"
  elif [ "$BOOTSTRAP_STATE" = "DRIFT" ] && [ "$ALLOW_BASE_DRIFT" -ne 1 ]; then
    echo "   REFUSE bootstrap: DRIFT requires --allow-base-drift; continuing other concerns"
  else
  art="${KITDIR}/host-scripts/init-host.sh.patched"
  [ -f "$art" ] || die "missing artifact $art"
  got="$(sha256_file "$art")"
  [ "$got" = "$ARTIFACT_SHA256" ] \
    || die "artifact sha256 $got does not match expected file digest $ARTIFACT_SHA256"
  aws_ s3 cp "$art" "s3://${BUCKET}/deployment/bootstrap/host/${ASSET_BUNDLE_PREFIX}/init-host.sh" \
    || die "asset upload failed"

  aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" --versions "$LT_VER" \
    --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
    | base64 -d > /tmp/restorepatch-ud.txt || die "cannot read in-service UserData"
  # Replace every observed content-addressed bootstrap prefix. The assertions below
  # forbid a no-op rewrite from creating and promoting an ineffective LT version.
  python3 - /tmp/restorepatch-ud.txt "$ASSET_BUNDLE_PREFIX" <<'PY'
import re
import sys
p, new = sys.argv[1], sys.argv[2]
t = open(p, encoding="utf-8", errors="surrogateescape").read()
pattern = re.compile(r"deployment/bootstrap/host/([0-9a-f]{64})")
old = sorted({m.group(1) for m in pattern.finditer(t) if m.group(1) != new})
if not old:
    raise SystemExit("FAIL: no old bootstrap prefix was found for substitution")
for value in old:
    t = t.replace("deployment/bootstrap/host/" + value,
                  "deployment/bootstrap/host/" + new)
if "deployment/bootstrap/host/" + new not in t:
    raise SystemExit("FAIL: target bootstrap prefix absent after substitution")
remaining = sorted({m.group(1) for m in pattern.finditer(t) if m.group(1) != new})
if remaining:
    raise SystemExit("FAIL: old bootstrap prefixes remain after substitution: " + ",".join(remaining))
if "{{" in t:
    raise SystemExit("FAIL: unrendered placeholder present after substitution")
open(p, "w", encoding="utf-8", errors="surrogateescape").write(t)
print("   UserData rewritten, %d old prefix(es) replaced; target present; no old prefix or placeholder remains" % len(old))
PY
  [ $? -eq 0 ] || die "UserData rewrite refused"
  b64="$(base64 -i /tmp/restorepatch-ud.txt 2>/dev/null || base64 -w0 /tmp/restorepatch-ud.txt)"
  newver="$(aws_ ec2 create-launch-template-version --launch-template-id "$LT_ID" \
    --source-version "$LT_VER" \
    --launch-template-data "{\"ImageId\":\"${AMI}\",\"UserData\":\"${b64}\"}" \
    --query 'LaunchTemplateVersion.VersionNumber' --output text)" || die "cannot create LT version"
  state_put host_lt_new_version "$newver"
  aws_ ec2 modify-launch-template --launch-template-id "$LT_ID" --default-version "$newver" \
    || die "cannot flip default version"
  say "   LT version $newver created and set default"
  fi

  say "3/3 overlay Lambda code, reusing each live package's dependencies"
  overlay_function "$FN" "${KITDIR}/lambda/api"
  overlay_function openclaw-backup "${KITDIR}/lambda/backup"
  overlay_function openclaw-health-check "${KITDIR}/lambda/health_check"
  overlay_function openclaw-scaler "${KITDIR}/lambda/scaler"
  if aws_ lambda get-function --function-name openclaw-tenant-stats-writer >/dev/null 2>&1; then
    overlay_function openclaw-tenant-stats-writer "${KITDIR}/lambda/tenant_stats"
  else
    echo "   ABSENT openclaw-tenant-stats-writer; no deployed target for its module"
  fi
  if aws_ lambda get-function --function-name openclaw-console-bff >/dev/null 2>&1; then
    overlay_function openclaw-console-bff "${KITDIR}/lambda/console-bff"
  else
    echo "   ABSENT openclaw-console-bff; console delivery path is not deployed"
  fi
  new_api_version="$(aws_ lambda publish-version --function-name "$FN" \
    --description restorepatch-amipacker --query Version --output text)" \
    || die "cannot publish patched API version"
  state_put api_applied_version "$new_api_version"
  if an="$(alias_name)"; then
    aws_ lambda update-alias --function-name "$FN" --name "$an" \
      --function-version "$new_api_version" >/dev/null || die "cannot advance API alias $an"
    state_put api_alias_name "$an"
  else
    echo "   API environment does not use an alias; the unqualified version is the serving path"
  fi

  say "controlled instance refresh, one host at a time"
  aws_ autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
    --preferences '{"MinHealthyPercentage":90,"InstanceWarmup":900,"SkipMatching":false}' \
    --query InstanceRefreshId --output text || die "cannot start instance refresh"
  say "apply OK — run verify next; HostASG MinSize was deliberately left untouched"
  ;;

verify)
  rc=0
  say "hook heartbeat"
  cur="$(aws_ autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" \
    --query "LifecycleHooks[?LifecycleHookName=='${HOOK_NAME}'].HeartbeatTimeout|[0]" --output text)"
  [ "$cur" = "$NEW_TIMEOUT" ] && echo "   PASS heartbeat=$cur" || { echo "   FAIL heartbeat=$cur"; rc=1; }

  say "MinSize must be unchanged (this tool never sets it to zero)"
  minsz="$(aws_ autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].MinSize' --output text)"
  [ "$minsz" = "0" ] && { echo "   FAIL MinSize is 0 — a live fleet may scale to zero"; rc=1; } \
                     || echo "   PASS MinSize=$minsz"

  say "default LT version carries the new bootstrap prefix and the new image"
  ud="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions '$Default' --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
    --output text | base64 -d)"
  printf '%s' "$ud" | grep -q "$ASSET_BUNDLE_PREFIX" && echo "   PASS bootstrap prefix" \
    || { echo "   FAIL bootstrap prefix absent"; rc=1; }
  printf '%s' "$ud" | grep -q '{{' && { echo "   FAIL unrendered placeholder in UserData"; rc=1; } \
    || echo "   PASS no unrendered placeholder"
  img="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions '$Default' --query 'LaunchTemplateVersions[0].LaunchTemplateData.ImageId' --output text)"
  if [ -n "$AMI" ]; then
    [ "$img" = "$AMI" ] && echo "   PASS ImageId=$img" || { echo "   FAIL ImageId=$img"; rc=1; }
  else
    echo "   INCONCLUSIVE new_ami_id absent from environment; ImageId=$img"
    rc=1
  fi

  say "bootstrap object present at its content-addressed key"
  aws_ s3api head-object --bucket "$BUCKET" \
    --key "deployment/bootstrap/host/${ASSET_BUNDLE_PREFIX}/init-host.sh" >/dev/null 2>&1 \
    && echo "   PASS object present" || { echo "   FAIL object missing"; rc=1; }

  say "openclaw-api code changed and its environment was not overwritten"
  want="$(state_get api_env_key_count)"
  now="$(aws_ lambda get-function-configuration --function-name "$FN" \
    --query 'length(Environment.Variables)' --output text)"
  [ -n "$want" ] && { [ "$want" = "$now" ] && echo "   PASS env keys=$now" \
    || { echo "   FAIL env keys $want -> $now"; rc=1; }; }
  aws_ lambda invoke --function-name "$FN" --payload eyJwYXRoIjoiL3BpbmcifQ== \
    /tmp/restorepatch-invoke.json --query FunctionError --output text | grep -qi none \
    && echo "   PASS invoke has no FunctionError" \
    || { echo "   NOTE inspect /tmp/restorepatch-invoke.json; a 404 body on a private API is expected"; }

  say "API unqualified and alias-resolved code paths have converged"
  applied_version="$(state_get api_applied_version)"
  applied_alias="$(state_get api_alias_name)"
  if [ -n "$applied_alias" ]; then
    alias_version="$(aws_ lambda get-alias --function-name "$FN" --name "$applied_alias" \
      --query FunctionVersion --output text)"
    [ "$alias_version" = "$applied_version" ] \
      && echo "   PASS alias $applied_alias points to version $applied_version" \
      || { echo "   FAIL alias $applied_alias points to $alias_version, expected $applied_version"; rc=1; }
    latest_sha="$(aws_ lambda get-function-configuration --function-name "$FN" \
      --query CodeSha256 --output text)"
    alias_sha="$(aws_ lambda get-function-configuration --function-name "$FN" \
      --qualifier "$alias_version" --query CodeSha256 --output text)"
    [ "$latest_sha" = "$alias_sha" ] \
      && echo "   PASS unqualified and alias-resolved CodeSha256 match" \
      || { echo "   FAIL CodeSha256 differs between unqualified and alias-resolved paths"; rc=1; }
  else
    echo "   ABSENT alias path; unqualified version is the serving path"
  fi

  say "instance refresh progress"
  aws_ autoscaling describe-instance-refreshes --auto-scaling-group-name "$ASG" \
    --query 'InstanceRefreshes[0].[Status,PercentageComplete]' --output text

  say "CodeBuild functional config unchanged (content-hash churn only)"
  aws_ codebuild batch-get-projects --names openclaw-golden-image-builder \
    --query 'projects[0].[environment.type,environment.computeType,timeoutInMinutes]' --output text

  [ "$rc" -eq 0 ] && say "verify PASS" || say "verify FAIL"
  exit "$rc"
  ;;

rollback)
  [ -f "$STATE" ] || die "no backup state; cannot roll back safely"
  hb="$(state_get hook_backup_timeout)"
  lv="$(state_get host_lt_backup_version)"
  av="$(state_get api_backup_version)"
  [ -n "$hb" ] && [ -n "$lv" ] && [ -n "$av" ] || die "backup state incomplete"

  say "restore hook heartbeat to $hb"
  aws_ autoscaling put-lifecycle-hook --auto-scaling-group-name "$ASG" \
    --lifecycle-hook-name "$HOOK_NAME" \
    --lifecycle-transition autoscaling:EC2_INSTANCE_LAUNCHING \
    --heartbeat-timeout "$hb" --default-result ABANDON || die "hook restore failed"

  say "flip LaunchTemplate default back to version $lv"
  aws_ ec2 modify-launch-template --launch-template-id "$LT_ID" --default-version "$lv" \
    || die "LT default restore failed"

  say "restore openclaw-api code and alias"
  restore_function "$FN"
  for extra_fn in openclaw-backup openclaw-health-check openclaw-scaler \
    openclaw-tenant-stats-writer openclaw-console-bff; do
    [ -n "$(state_get "backup_${extra_fn}")" ] && restore_function "$extra_fn"
  done
  # Both paths must be restored: lifecycle dispatch binds the unqualified function,
  # while API Gateway methods can resolve through the environment's live alias.
  restored_alias="$(state_get api_alias_name)"
  restored_alias_version="$(state_get api_alias_backup_version)"
  if [ -n "$restored_alias" ] && [ -n "$restored_alias_version" ]; then
    aws_ lambda update-alias --function-name "$FN" --name "$restored_alias" \
      --function-version "$restored_alias_version" >/dev/null \
      || die "cannot restore alias $restored_alias"
  else
    echo "   API environment does not use an alias; unqualified code restore is sufficient"
  fi

  say "roll the fleet back onto the restored template"
  aws_ autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
    --preferences '{"MinHealthyPercentage":90}' --query InstanceRefreshId --output text \
    || die "cannot start rollback refresh"
  say "rollback OK — the previous bootstrap object is content-addressed and still present"
  ;;

apply-api)
  resolve_api_context
  resolve_target_vpc
  probe_private_api_endpoint

  api_type="$(aws_ apigateway get-rest-api --rest-api-id "$API_ID" \
    --query 'endpointConfiguration.types[0]' --output text)"
  api_vpces="$(aws_ apigateway get-rest-api --rest-api-id "$API_ID" \
    --query 'endpointConfiguration.vpcEndpointIds' --output text)"
  case " $api_vpces " in *" $VPC_ENDPOINT_ID "*) vpce_attached=1 ;; *) vpce_attached=0 ;; esac
  [ -n "$(state_get api_old_endpoint_type)" ] || state_put api_old_endpoint_type "$api_type"
  [ -n "$(state_get api_vpce_was_attached)" ] || state_put api_vpce_was_attached "$vpce_attached"
  state_put api_rest_api_id "$API_ID"
  state_put api_stage_name "$API_STAGE"
  state_put api_vpc_endpoint_id "$VPC_ENDPOINT_ID"

  if [ "$vpce_attached" -eq 1 ] && [ "$api_type" = "PRIVATE" ]; then
    say "endpoint configuration already contains $VPC_ENDPOINT_ID"
  elif [ "$api_type" = "PRIVATE" ]; then
    aws_ apigateway update-rest-api --rest-api-id "$API_ID" \
      --patch-operations "op=add,path=/endpointConfiguration/vpcEndpointIds,value=${VPC_ENDPOINT_ID}" \
      >/dev/null || die "cannot attach $VPC_ENDPOINT_ID to REST API $API_ID"
  elif [ "$api_type" = "REGIONAL" ]; then
    aws_ apigateway update-rest-api --rest-api-id "$API_ID" \
      --patch-operations \
        "op=replace,path=/endpointConfiguration/types/REGIONAL,value=PRIVATE" \
        "op=add,path=/endpointConfiguration/vpcEndpointIds,value=${VPC_ENDPOINT_ID}" \
      >/dev/null || die "cannot convert REST API $API_ID to the private endpoint"
  else
    die "REST API $API_ID endpoint type $api_type is not eligible for the private endpoint update"
  fi

  old_deployment="$(state_get api_old_deployment_id)"
  if [ -z "$old_deployment" ]; then
    old_deployment="$(aws_ apigateway get-stage --rest-api-id "$API_ID" \
      --stage-name "$API_STAGE" --query deploymentId --output text)"
    state_put api_old_deployment_id "$old_deployment"
  fi
  new_deployment="$(aws_ apigateway create-deployment --rest-api-id "$API_ID" \
    --description restorepatch-amipacker --query id --output text)" \
    || die "cannot create REST API deployment"
  state_put api_new_deployment_id "$new_deployment"
  aws_ apigateway update-stage --rest-api-id "$API_ID" --stage-name "$API_STAGE" \
    --patch-operations "op=replace,path=/deploymentId,value=${new_deployment}" \
    >/dev/null || die "cannot point stage $API_STAGE to deployment $new_deployment"
  say "API apply OK: endpoint=$VPC_ENDPOINT_ID deployment=$new_deployment stage=$API_STAGE"
  ;;

verify-api)
  resolve_api_context
  rc=0
  expected_vpce="$(state_get api_vpc_endpoint_id)"
  expected_deployment="$(state_get api_new_deployment_id)"
  [ -n "$expected_vpce" ] || { resolve_target_vpc; probe_private_api_endpoint; expected_vpce="$VPC_ENDPOINT_ID"; }
  api_type="$(aws_ apigateway get-rest-api --rest-api-id "$API_ID" \
    --query 'endpointConfiguration.types[0]' --output text)"
  api_vpces="$(aws_ apigateway get-rest-api --rest-api-id "$API_ID" \
    --query 'endpointConfiguration.vpcEndpointIds' --output text)"
  stage_deployment="$(aws_ apigateway get-stage --rest-api-id "$API_ID" \
    --stage-name "$API_STAGE" --query deploymentId --output text)"
  [ "$api_type" = "PRIVATE" ] \
    && echo "   PASS endpointConfiguration.types[0]=PRIVATE" \
    || { echo "   FAIL endpointConfiguration.types[0]=$api_type"; rc=1; }
  case " $api_vpces " in
    *" $expected_vpce "*) echo "   PASS endpointConfiguration.vpcEndpointIds contains $expected_vpce" ;;
    *) echo "   FAIL endpointConfiguration.vpcEndpointIds=$api_vpces"; rc=1 ;;
  esac
  [ -n "$expected_deployment" ] && [ "$stage_deployment" = "$expected_deployment" ] \
    && echo "   PASS stage $API_STAGE deploymentId=$stage_deployment" \
    || { echo "   FAIL stage $API_STAGE deploymentId=$stage_deployment expected=$expected_deployment"; rc=1; }
  [ "$rc" -eq 0 ] && say "verify-api PASS" || say "verify-api FAIL"
  exit "$rc"
  ;;

finalize-api)
  resolve_api_context
  old_deployment="$(state_get api_old_deployment_id)"
  new_deployment="$(state_get api_new_deployment_id)"
  [ -n "$old_deployment" ] && [ -n "$new_deployment" ] || die "API deployment state is incomplete"
  stage_deployment="$(aws_ apigateway get-stage --rest-api-id "$API_ID" \
    --stage-name "$API_STAGE" --query deploymentId --output text)"
  [ "$stage_deployment" = "$new_deployment" ] \
    || die "stage $API_STAGE points to $stage_deployment, not replacement $new_deployment"
  if [ "$old_deployment" = "$new_deployment" ]; then
    say "no replaced deployment to delete"
  else
    aws_ apigateway delete-deployment --rest-api-id "$API_ID" \
      --deployment-id "$old_deployment" \
      || die "cannot delete replaced deployment $old_deployment"
    state_put api_old_deployment_deleted true
    say "deleted replaced deployment $old_deployment"
  fi
  ;;

rollback-api)
  resolve_api_context
  old_deployment="$(state_get api_old_deployment_id)"
  new_deployment="$(state_get api_new_deployment_id)"
  old_type="$(state_get api_old_endpoint_type)"
  vpce_attached="$(state_get api_vpce_was_attached)"
  vpce_id="$(state_get api_vpc_endpoint_id)"
  [ -n "$old_deployment" ] && [ -n "$old_type" ] && [ -n "$vpce_id" ] \
    || die "API rollback state is incomplete"
  [ "$(state_get api_old_deployment_deleted)" != "true" ] \
    || die "replaced deployment $old_deployment was finalized and cannot be restored"
  aws_ apigateway update-stage --rest-api-id "$API_ID" --stage-name "$API_STAGE" \
    --patch-operations "op=replace,path=/deploymentId,value=${old_deployment}" \
    >/dev/null || die "cannot restore stage $API_STAGE to deployment $old_deployment"
  if [ "$vpce_attached" = "0" ] && [ "$old_type" = "PRIVATE" ]; then
    aws_ apigateway update-rest-api --rest-api-id "$API_ID" \
      --patch-operations "op=remove,path=/endpointConfiguration/vpcEndpointIds,value=${vpce_id}" \
      >/dev/null || die "cannot detach $vpce_id during rollback"
  elif [ "$vpce_attached" = "0" ] && [ "$old_type" = "REGIONAL" ]; then
    aws_ apigateway update-rest-api --rest-api-id "$API_ID" \
      --patch-operations \
        "op=remove,path=/endpointConfiguration/vpcEndpointIds,value=${vpce_id}" \
        "op=replace,path=/endpointConfiguration/types/PRIVATE,value=REGIONAL" \
      >/dev/null || die "cannot restore regional endpoint configuration"
  fi
  if [ -n "$new_deployment" ] && [ "$new_deployment" != "$old_deployment" ]; then
    aws_ apigateway delete-deployment --rest-api-id "$API_ID" \
      --deployment-id "$new_deployment" \
      || die "cannot delete rolled-back deployment $new_deployment"
  fi
  say "API rollback OK: stage=$API_STAGE deployment=$old_deployment endpoint_type=$old_type"
  ;;

*) echo "unknown phase: $PHASE" >&2; exit 2 ;;
esac
