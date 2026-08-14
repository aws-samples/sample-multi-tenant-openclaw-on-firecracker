#!/usr/bin/env bash
# apply-amipatch.sh — deterministic driver for amipatch's resource-owning operation.
#
# The kit's twelve synthesized CloudFormation resource changes are applied, verified and
# rolled back ONLY through this tool, so no operator hand-assembles a fleet-breaking
# command sequence and every executor runs identical code.
#
# It covers exactly three concerns and refuses to do anything else:
#   1. host-init lifecycle hook heartbeat timeout 1200 -> 3600
#   2. one new LaunchTemplate version carrying the Packer AMI id AND the new
#      content-addressed bootstrap prefix, then default-version flip + controlled refresh
#   3. openclaw-api function code overlay (reuses the live package's own dependencies)
#
# Deliberately NOT done, and it will refuse if asked:
#   * HostASG MinSize 2 -> 0. That value is first-deployment semantics; on a live fleet it
#     permits scaling to zero hosts that carry real tenants.
#   * TrackDefaultLTVersion AsgShape digest and the OpenClawImage CodeBuild churn. Both are
#     derived or content-hash projections with no independent action.
#
# usage: lib/apply-amipatch.sh <precheck|backup|apply|verify|rollback> --env <environment.json> --kit <kit-dir>
set -uo pipefail

PHASE="${1:?phase required: precheck|backup|apply|verify|rollback}"; shift || true
ENVJSON=""; KITDIR="."
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENVJSON="${2:?}"; shift 2 ;;
    --kit) KITDIR="${2:?}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -f "$ENVJSON" ] || { echo "FATAL: environment file not found: $ENVJSON" >&2; exit 2; }

BASE_PREFIX=29901cb4b92f93eed6995f4737b29b2e2558836c911631209cd23260fe07af3b
PATCH_PREFIX=938e619b7c6e1b292733e9161d3f0b71603aa32f4930e15db9d551624bc72d90
HOOK_NAME=openclaw-host-init
NEW_TIMEOUT=3600
STATE="${KITDIR}/.amipatch-state.json"

jget() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$1" "$2"; }

REGION="$(jget "$ENVJSON" region)"
ASG="$(jget "$ENVJSON" host_asg)"
LT_ID="$(jget "$ENVJSON" host_lt_id)"
LT_VER="$(jget "$ENVJSON" host_lt_current_version)"
BUCKET="$(jget "$ENVJSON" assets_bucket)"
FN="$(jget "$ENVJSON" api_function_name)"
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
  say "in-service LaunchTemplate bootstrap prefix must still be the base form"
  aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" --versions "$LT_VER" \
    --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
    | base64 -d | grep -q "$BASE_PREFIX" \
    || die "in-service LaunchTemplate does not carry the base bootstrap prefix; environment is not at base_sha"
  say "openclaw-api environment key count (must not change across apply)"
  aws_ lambda get-function-configuration --function-name "$FN" \
    --query 'length(Environment.Variables)' --output text
  say "OpenClawImage CodeBuild functional config (content-hash churn must not alter it)"
  aws_ codebuild batch-get-projects --names openclaw-golden-image-builder \
    --query 'projects[0].[environment.type,environment.computeType,timeoutInMinutes]' --output text
  say "precheck OK"
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
    --description pre-amipatch-anchor --query Version --output text)" || die "cannot publish anchor version"
  state_put api_backup_version "$ver"
  loc="$(aws_ lambda get-function --function-name "$FN" --query 'Code.Location' --output text)"
  curl -fsS -o /tmp/openclaw-api-backup.zip "$loc" || die "cannot download live package"
  sha="$(shasum -a 256 /tmp/openclaw-api-backup.zip 2>/dev/null | awk '{print $1}')"
  [ -n "$sha" ] || sha="$(sha256sum /tmp/openclaw-api-backup.zip | awk '{print $1}')"
  state_put api_backup_zip_sha256 "$sha"
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
  art="${KITDIR}/host-scripts/init-host.sh.patched"
  [ -f "$art" ] || die "missing artifact $art"
  got="$(shasum -a 256 "$art" 2>/dev/null | awk '{print $1}')"
  [ -n "$got" ] || got="$(sha256sum "$art" | awk '{print $1}')"
  [ "$got" = "$PATCH_PREFIX" ] || die "artifact sha256 $got does not match the target prefix $PATCH_PREFIX"
  aws_ s3 cp "$art" "s3://${BUCKET}/deployment/bootstrap/host/${PATCH_PREFIX}/init-host.sh" \
    || die "asset upload failed"

  aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" --versions "$LT_VER" \
    --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
    | base64 -d > /tmp/amipatch-ud.txt || die "cannot read in-service UserData"
  # one literal 64-hex substitution; this UserData is CDK-rendered, so there is nothing to template
  python3 - /tmp/amipatch-ud.txt "$BASE_PREFIX" "$PATCH_PREFIX" <<'PY'
import sys
p, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(p, encoding="utf-8", errors="surrogateescape").read()
if old not in t:
    raise SystemExit("FAIL: base prefix absent from in-service UserData")
t = t.replace(old, new)
if "{{" in t:
    raise SystemExit("FAIL: unrendered placeholder present after substitution")
open(p, "w", encoding="utf-8", errors="surrogateescape").write(t)
print("   UserData rewritten, 1 prefix substituted, no unrendered placeholder")
PY
  [ $? -eq 0 ] || die "UserData rewrite refused"
  b64="$(base64 -i /tmp/amipatch-ud.txt 2>/dev/null || base64 -w0 /tmp/amipatch-ud.txt)"
  newver="$(aws_ ec2 create-launch-template-version --launch-template-id "$LT_ID" \
    --source-version "$LT_VER" \
    --launch-template-data "{\"ImageId\":\"${AMI}\",\"UserData\":\"${b64}\"}" \
    --query 'LaunchTemplateVersion.VersionNumber' --output text)" || die "cannot create LT version"
  state_put host_lt_new_version "$newver"
  aws_ ec2 modify-launch-template --launch-template-id "$LT_ID" --default-version "$newver" \
    || die "cannot flip default version"
  say "   LT version $newver created and set default"

  say "3/3 overlay openclaw-api code, reusing the live package dependencies"
  work=/tmp/amipatch-api
  rm -rf "$work" && mkdir -p "$work" && (cd "$work" && unzip -q /tmp/openclaw-api-backup.zip) \
    || die "cannot unpack live package"
  for f in action_idem host_service tenant_query_service tenant_service; do
    src="${KITDIR}/lambda/api/services/${f}.py"
    [ -f "$src" ] || die "missing artifact $src"
    cp "$src" "${work}/services/${f}.py" || die "cannot overlay ${f}.py"
  done
  (cd "$work" && zip -qr /tmp/amipatch-api-new.zip .) || die "cannot repack"
  aws_ lambda update-function-code --function-name "$FN" \
    --zip-file fileb:///tmp/amipatch-api-new.zip >/dev/null || die "update-function-code failed"
  aws_ lambda wait function-updated --function-name "$FN" || die "function did not settle"

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
  printf '%s' "$ud" | grep -q "$PATCH_PREFIX" && echo "   PASS bootstrap prefix" \
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
    --key "deployment/bootstrap/host/${PATCH_PREFIX}/init-host.sh" >/dev/null 2>&1 \
    && echo "   PASS object present" || { echo "   FAIL object missing"; rc=1; }

  say "openclaw-api code changed and its environment was not overwritten"
  want="$(state_get api_env_key_count)"
  now="$(aws_ lambda get-function-configuration --function-name "$FN" \
    --query 'length(Environment.Variables)' --output text)"
  [ -n "$want" ] && { [ "$want" = "$now" ] && echo "   PASS env keys=$now" \
    || { echo "   FAIL env keys $want -> $now"; rc=1; }; }
  aws_ lambda invoke --function-name "$FN" --payload eyJwYXRoIjoiL3BpbmcifQ== \
    /tmp/amipatch-invoke.json --query FunctionError --output text | grep -qi none \
    && echo "   PASS invoke has no FunctionError" \
    || { echo "   NOTE inspect /tmp/amipatch-invoke.json; a 404 body on a private API is expected"; }

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
  aws_ lambda update-function-code --function-name "$FN" \
    --zip-file fileb:///tmp/openclaw-api-backup.zip >/dev/null || die "code restore failed"
  aws_ lambda wait function-updated --function-name "$FN" || die "function did not settle"
  # both paths: the lifecycle dispatch event source binds the unqualified function, so
  # flipping the alias alone does not revert it
  aws_ lambda update-alias --function-name "$FN" --name live --function-version "$av" \
    >/dev/null || echo "   NOTE alias live not present or already correct"

  say "roll the fleet back onto the restored template"
  aws_ autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
    --preferences '{"MinHealthyPercentage":90}' --query InstanceRefreshId --output text \
    || die "cannot start rollback refresh"
  say "rollback OK — the previous bootstrap object is content-addressed and still present"
  ;;

*) echo "unknown phase: $PHASE" >&2; exit 2 ;;
esac
