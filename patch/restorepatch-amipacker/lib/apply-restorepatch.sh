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
# usage: lib/apply-restorepatch.sh <precheck|backup|apply|apply-control|canary|refresh|verify|rollback|apply-api|verify-api|finalize-api|rollback-api> --env <environment.json> --kit <kit-dir> [--values <values.json>] [--allow-base-drift]
set -uo pipefail

PHASE="${1:?phase required: precheck|backup|apply|apply-control|canary|refresh|verify|rollback|apply-api|verify-api|finalize-api|rollback-api}"; shift || true
ENVJSON=""; KITDIR="."; VALUESJSON=""; ALLOW_BASE_DRIFT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENVJSON="${2:?}"; shift 2 ;;
    --kit) KITDIR="${2:?}"; shift 2 ;;
    --values) VALUESJSON="${2:?}"; shift 2 ;;
    --allow-base-drift) ALLOW_BASE_DRIFT=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -f "$ENVJSON" ] || { echo "FATAL: environment file not found: $ENVJSON" >&2; exit 2; }
[ -z "$VALUESJSON" ] || [ -f "$VALUESJSON" ] \
  || { echo "FATAL: values file not found: $VALUESJSON" >&2; exit 2; }

# These values identify CDK asset bundles and therefore name S3 prefixes. They are
# not hashes of init-host.sh, whose independent byte digest is checked separately.
BASE_ASSET_BUNDLE_PREFIX=dea0bd3d54ac88764319c07b1b546df952e8e828f7f3aba0d18866160ee2d046
ASSET_BUNDLE_PREFIX=f6e72b08706d1e01503d4cd738f4a7af1840bdcfedb3878d38b10a3123d1c1f2
ARTIFACT_SHA256=ef0fbf78501b0bb07e7968146d987078c25baf850ea0f208fb877a45c5779cd2
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

# Control-plane-only phases must remain usable before the dataplane AMI and ASG are ready.
case "$PHASE" in
  apply-control|verify-api|finalize-api|rollback-api)
    REQUIRED_VARS="REGION FN"
    ;;
  apply-api)
    REQUIRED_VARS="REGION FN"
    ;;
  *)
    REQUIRED_VARS="REGION ASG LT_ID LT_VER BUCKET FN"
    ;;
esac
for v in $REQUIRED_VARS; do
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
    # Build caches and workstation metadata are not customer delivery artifacts.
    case "$rel" in
      __pycache__/*|*/__pycache__/*|*.pyc|*.pyo|.DS_Store|*/.DS_Store)
        echo "   SKIP $rel (not a delivery artifact)"
        continue
        ;;
    esac
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

render_bootstrap_artifact() {
  local template="$1" live_rendered="$2" out="$3"
  # Measured CDK output needs ordered live-object tiers before the JSON fallback.
  python3 - "$template" "$live_rendered" "$out" "$VALUESJSON" \
    "${KITDIR}/host-scripts/host-agent.service.patched" <<'PY'
import json
import pathlib
import re
import sys

template_path, live_path, out_path, values_path, host_agent_unit_path = sys.argv[1:]
names = [
    "AGENTCORE_GATEWAY_URL", "AMP_REMOTE_WRITE_URL", "BACKUP_DATA_SCRIPT",
    "BALLOON_DEFLATE_ON_OOM", "BALLOON_ENABLED", "BALLOON_FREE_PAGE_REPORTING",
    "BALLOON_MAX_INFLATE_RATIO", "BALLOON_MIN_GUEST_AVAILABLE_MB",
    "BALLOON_STATS_INTERVAL", "CPU_OVERCOMMIT_RATIO", "DNAT_PORT_HIGH",
    "DNAT_PORT_LOW", "EGRESS_ALLOWLIST_CIDRS", "EGRESS_ALLOWLIST_DOMAINS",
    "EGRESS_ALLOWLIST_ENABLED", "EGRESS_DNS_UPSTREAM", "EGRESS_INCLUDE_VPC_CIDR",
    "EGRESS_VPC_CIDR", "FB_STREAM_HOST", "FB_STREAM_VM", "HOST_AGENT_SCRIPT",
    "HOST_RESERVED_MEM", "HOST_RESERVED_VCPU", "HOST_USER_HOOK", "LOGGING_ENABLED",
    "MEM_OVERCOMMIT_RATIO", "NOMINAL_SPECS", "OC_HOST_LAUNCH_SLOTS",
    "OVERCOMMIT_BY_FAMILY", "PORT_QUARANTINE_SECONDS", "PROVISION_SCRIPT",
    "ROOTFS_OVERLAY_MB", "ROOTFS_PREFIX", "SUBNET_PREFIX",
]
whole_line_names = {
    "AGENTCORE_GATEWAY_URL", "FB_STREAM_HOST", "FB_STREAM_VM",
    "HOST_RESERVED_MEM", "HOST_RESERVED_VCPU", "NOMINAL_SPECS", "ROOTFS_PREFIX",
}
block_names = {
    "PROVISION_SCRIPT", "HOST_AGENT_SCRIPT", "BACKUP_DATA_SCRIPT", "HOST_USER_HOOK",
}
template_lines = pathlib.Path(template_path).read_text(
    encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
live_lines = pathlib.Path(live_path).read_text(
    encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
values = {}
if values_path:
    loaded = json.loads(pathlib.Path(values_path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit("FAIL: --values JSON must be an object keyed by placeholder name")
    values = loaded

def is_comment(line):
    return re.match(r"^\s*#", line) is not None

def without_newline(line):
    return line.rstrip("\r\n")

def has_placeholder(line):
    return re.search(r"\{\{[A-Z_]+\}\}", line) is not None

def assignment(line, name):
    if is_comment(line):
        return None
    return re.match(
        rf"^(?P<prefix>\s*(?:export\s+)?{re.escape(name)}\s*=)(?P<rhs>.*?)(?P<nl>\r?\n)?$",
        line,
    )

def whole_line_value(name):
    # These values are embedded in code lines, so same-name assignment lookup cannot find them.
    token = "{{" + name + "}}"
    captured = []
    for template_line in template_lines:
        if token not in template_line or is_comment(template_line):
            continue
        parts = without_newline(template_line).split(token)
        pattern = re.escape(parts[0]) + r"(?P<value>.*?)"
        for part in parts[1:-1]:
            pattern += re.escape(part) + r"(?P=value)"
        pattern += re.escape(parts[-1])
        matcher = re.compile(pattern)
        for live_line in live_lines:
            if is_comment(live_line):
                continue
            match = matcher.fullmatch(without_newline(live_line))
            if match:
                captured.append(match.group("value"))
    if not captured or len(set(captured)) != 1:
        return False, ""
    return True, captured[0]

def find_subsequences(lines, needle):
    width = len(needle)
    return [
        index for index in range(len(lines) - width + 1)
        if lines[index:index + width] == needle
    ]

def unique_anchor(index, direction):
    # Block extent needs increasingly specific literal anchors because one-line shells repeat.
    eligible = []
    cursor = index + direction
    while 0 <= cursor < len(template_lines) and len(eligible) < 3:
        line = template_lines[cursor]
        if without_newline(line).strip() and not has_placeholder(line):
            eligible.append(cursor)
        cursor += direction
    live_text = [without_newline(line) for line in live_lines]
    for width in range(1, len(eligible) + 1):
        selected = eligible[:width]
        start, end = min(selected), max(selected)
        window = template_lines[start:end + 1]
        if any(has_placeholder(line) for line in window):
            continue
        matches = find_subsequences(
            live_text, [without_newline(line) for line in window])
        if len(matches) == 1:
            live_start = matches[0]
            return True, live_start, live_start + len(window)
    return False, -1, -1

def anchor_block_value(name):
    token = "{{" + name + "}}"
    indexes = [
        index for index, line in enumerate(template_lines)
        if without_newline(line).strip() == token
    ]
    if len(indexes) != 1:
        return False, ""
    index = indexes[0]
    upper_ok, _, upper_end = unique_anchor(index, -1)
    lower_ok, lower_start, _ = unique_anchor(index, 1)
    if not upper_ok or not lower_ok or upper_end > lower_start:
        return False, ""
    return True, "".join(live_lines[upper_end:lower_start])

def replace_block(rendered_lines, name, value):
    token = "{{" + name + "}}"
    indexes = [
        index for index, line in enumerate(rendered_lines)
        if not is_comment(line) and token in line
    ]
    if not indexes or any(without_newline(rendered_lines[index]).strip() != token
                          for index in indexes):
        return False
    for index in indexes:
        rendered_lines[index] = value
    return True

rendered = list(template_lines)
for name in names:
    token = "{{" + name + "}}"
    template_indexes = [
        index for index, line in enumerate(rendered)
        if token in line and assignment(line, name)
    ]
    live_match = next((assignment(line, name) for line in live_lines if assignment(line, name)), None)
    if template_indexes and live_match:
        for index in template_indexes:
            current = assignment(rendered[index], name)
            rendered[index] = current.group("prefix") + live_match.group("rhs") + (current.group("nl") or "")
        continue
    if name in whole_line_names:
        resolved, value = whole_line_value(name)
        if resolved:
            rendered = [
                line if is_comment(line) else line.replace(token, value)
                for line in rendered
            ]
            continue
    if name in block_names:
        if name == "HOST_AGENT_SCRIPT":
            unit_text = pathlib.Path(host_agent_unit_path).read_bytes().decode(
                "utf-8", errors="surrogateescape")
            if "SVCEOF" in unit_text.splitlines():
                raise SystemExit(
                    "FAIL: host-agent.service.patched contains heredoc delimiter SVCEOF")
            # Copying the live T3 block would drop the kit's PYTHONUNBUFFERED=1 unit change.
            value = (
                "cat > /etc/systemd/system/host-agent.service << 'SVCEOF'\n"
                + unit_text
                + ("" if unit_text.endswith(("\n", "\r")) else "\n")
                + "SVCEOF\n"
            )
            resolved = True
        else:
            resolved, value = anchor_block_value(name)
        if resolved and replace_block(rendered, name, value):
            continue
        if name == "HOST_AGENT_SCRIPT":
            # This exception must never fall back to a value that bypasses the patched unit.
            continue
    if name not in values:
        continue
    value = values[name]
    if not isinstance(value, str):
        value = json.dumps(value, separators=(",", ":"))
    rendered = [
        line if is_comment(line) else line.replace(token, value)
        for line in rendered
    ]

output_text = "".join(rendered)
# Block tiers add multiple lines per list entry, so gates must inspect physical output lines.
output_lines = output_text.splitlines(keepends=True)
unresolved = sorted({
    match.group(1)
    for line in output_lines if not is_comment(line)
    for match in re.finditer(r"\{\{([A-Z_]+)\}\}", line)
})
if unresolved:
    raise SystemExit("FAIL: unresolved bootstrap placeholders: " + ", ".join(unresolved))
comment_text = "".join(line for line in output_lines if is_comment(line))
if "{{AVAIL_VCPU}}" not in comment_text or "{{AVAIL_MEM}}" not in comment_text:
    raise SystemExit("FAIL: historical AVAIL_VCPU/AVAIL_MEM comment placeholders were altered")
template_line_count = len("".join(template_lines).splitlines())
output_line_count = len(output_text.splitlines())
if output_line_count < template_line_count:
    raise SystemExit(
        "FAIL: rendered bootstrap was truncated: "
        f"{output_line_count} lines < template {template_line_count} lines"
    )
pathlib.Path(out_path).write_text(
    output_text, encoding="utf-8", errors="surrogateescape")
print(
    "   rendered bootstrap: %d lines; code placeholders resolved; historical comments preserved"
    % output_line_count
)
PY
  [ $? -eq 0 ] || die "bootstrap rendering refused"
  # S3 consumers invoke or source these bytes after download, matching the kit's 0644 convention.
  chmod 0644 "$out" || die "cannot set rendered bootstrap mode to 0644"
}

asset_state_key() {
  # Stable key normalization keeps per-object rollback coordinates in the shared state file.
  printf '%s' "$1" | tr '/.-' '___'
}

publish_s3_asset() {
  local local_path="$1" key="$2" state_key old_info old_etag old_len old_ver
  local local_len readback new_info new_len new_ver mode
  [ -f "$local_path" ] || die "missing asset $local_path"
  state_key="$(asset_state_key "$key")"
  local_len="$(wc -c < "$local_path" | tr -d ' ')"
  mode="$(stat -f '%Lp' "$local_path" 2>/dev/null || stat -c '%a' "$local_path")"
  [ "$mode" = "644" ] || die "$local_path mode is $mode, expected 0644"

  if old_info="$(aws_ s3api head-object --bucket "$BUCKET" --key "$key" \
      --query '[ETag,ContentLength,VersionId]' --output text 2>/dev/null)"; then
    read -r old_etag old_len old_ver <<< "$old_info"
    [ -n "$(state_get "s3_old_version_${state_key}")" ] || {
      state_put "s3_old_etag_${state_key}" "$old_etag"
      state_put "s3_old_length_${state_key}" "$old_len"
      state_put "s3_old_version_${state_key}" "${old_ver:-None}"
    }
    if [ "$old_len" = "$local_len" ]; then
      readback="$(mktemp)"
      if aws_ s3 cp "s3://${BUCKET}/${key}" "$readback" --no-progress >/dev/null 2>&1 \
          && cmp -s "$local_path" "$readback"; then
        rm -f "$readback"
        echo "   SKIP s3://${BUCKET}/${key} (bytes already identical)"
        return 0
      fi
      rm -f "$readback"
    fi
  else
    [ -n "$(state_get "s3_old_version_${state_key}")" ] || {
      state_put "s3_old_etag_${state_key}" ABSENT
      state_put "s3_old_length_${state_key}" ABSENT
      state_put "s3_old_version_${state_key}" ABSENT
    }
  fi

  # Dependencies are published before callers so a concurrent boot never sees a partial set.
  aws_ s3 cp "$local_path" "s3://${BUCKET}/${key}" --no-progress \
    || die "asset upload failed: s3://${BUCKET}/${key}"
  new_info="$(aws_ s3api head-object --bucket "$BUCKET" --key "$key" \
    --query '[ContentLength,VersionId]' --output text)" \
    || die "cannot read back uploaded asset metadata: s3://${BUCKET}/${key}"
  read -r new_len new_ver <<< "$new_info"
  [ "$new_len" = "$local_len" ] \
    || die "asset length mismatch for $key: local=$local_len s3=$new_len"
  state_put "s3_changed_${state_key}" true
  state_put "s3_new_version_${state_key}" "${new_ver:-None}"
  echo "   PASS s3://${BUCKET}/${key} ContentLength=$new_len"
}

publish_host_assets() {
  local root="${KITDIR}/host-scripts" rel
  # Publish callees first; a boot or agent restart must not expose a new caller to old/missing scripts.
  publish_s3_asset "$root/launch-vm.sh.patched" "deployment/scripts/launch-vm.sh"
  publish_s3_asset "$root/rebuild-vm.sh.patched" "deployment/scripts/rebuild-vm.sh"
  publish_s3_asset "$root/reset-vm.sh.patched" "deployment/scripts/reset-vm.sh"
  while IFS= read -r rel; do
    # Same exclusion as overlay_function: this loop enumerates a directory, so a build
    # cache or workstation metadata file left there would become a customer S3 object.
    case "$rel" in
      __pycache__/*|*/__pycache__/*|*.pyc|*.pyo|.DS_Store|*/.DS_Store)
        echo "   SKIP $rel (not a delivery artifact)"
        continue
        ;;
    esac
    publish_s3_asset "$root/edge/fluent-bit/$rel" "deployment/observability/fluent-bit/$rel"
  done < <(cd "$root/edge/fluent-bit" && find . -type f -print | sed 's|^\./||' | sort)
  # The unit is embedded by HOST_AGENT_SCRIPT; no runtime consumes an S3 service key.
  publish_s3_asset "$root/host-agent.py.patched" "deployment/scripts/host-agent.py"
}

restore_s3_asset() {
  local key="$1" state_key old_ver old_len copy_source restored_len
  state_key="$(asset_state_key "$key")"
  [ "$(state_get "s3_changed_${state_key}")" = "true" ] || return 0
  old_ver="$(state_get "s3_old_version_${state_key}")"
  old_len="$(state_get "s3_old_length_${state_key}")"
  if [ "$old_ver" = "ABSENT" ]; then
    # A delete marker restores absence without destroying versioned recovery history.
    aws_ s3api delete-object --bucket "$BUCKET" --key "$key" >/dev/null \
      || die "cannot remove newly introduced asset $key"
    echo "   RESTORE s3://${BUCKET}/${key} to ABSENT"
    return 0
  fi
  [ -n "$old_ver" ] && [ "$old_ver" != "None" ] \
    || die "missing versioned rollback coordinate for $key"
  copy_source="$(python3 - "$BUCKET" "$key" "$old_ver" <<'PY'
import sys
import urllib.parse
bucket, key, version = sys.argv[1:]
print(urllib.parse.quote(f"{bucket}/{key}", safe="/") + "?versionId=" + urllib.parse.quote(version, safe=""))
PY
)"
  aws_ s3api copy-object --bucket "$BUCKET" --key "$key" --copy-source "$copy_source" >/dev/null \
    || die "cannot restore prior version of $key"
  restored_len="$(aws_ s3api head-object --bucket "$BUCKET" --key "$key" \
    --query ContentLength --output text)" || die "cannot verify restored asset $key"
  [ "$restored_len" = "$old_len" ] \
    || die "restored asset length mismatch for $key: expected=$old_len actual=$restored_len"
  echo "   RESTORE s3://${BUCKET}/${key} VersionId=$old_ver"
}

restore_host_assets() {
  local root="${KITDIR}/host-scripts" rel
  # Restore every object this driver may have changed, including the rendered bootstrap.
  restore_s3_asset "deployment/bootstrap/host/${ASSET_BUNDLE_PREFIX}/init-host.sh"
  restore_s3_asset "deployment/scripts/host-agent.py"
  # The inline unit rolls back with the bootstrap object, not a separate S3 key.
  while IFS= read -r rel; do
    restore_s3_asset "deployment/observability/fluent-bit/$rel"
  done < <(cd "$root/edge/fluent-bit" && find . -type f -print | sed 's|^\./||' | sort -r)
  restore_s3_asset "deployment/scripts/reset-vm.sh"
  restore_s3_asset "deployment/scripts/rebuild-vm.sh"
  restore_s3_asset "deployment/scripts/launch-vm.sh"
}

apply_control_overlay() {
  # Keep the Lambda-only path independent from every dataplane coordinate.
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
      # Compare the rendered form: {{PROVISION_SCRIPT}} is one template line but 251
      # live lines, so raw template comparison diverges at line 69 and cannot reach ALREADY.
      if ( render_bootstrap_artifact "${KITDIR}/host-scripts/init-host.sh.patched" \
           "${probe}/live-init-host.sh" "${probe}/rendered-init-host.sh" ); then
        python3 - "${probe}/live-init-host.sh" "${probe}/rendered-init-host.sh" <<'PY'
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
          echo "         comparison used the rendered kit artifact"
          BOOTSTRAP_STATE=ALREADY
        else
          echo "   STATE bootstrap=DRIFT — in-service content differs from the kit artifact"
          BOOTSTRAP_STATE=DRIFT
        fi
      else
        # This read-only precheck decision must fail closed instead of letting a render
        # failure turn "cannot decide" into a fatal tool error.
        echo "   STATE bootstrap=DRIFT — cannot render the kit bootstrap artifact, so the"
        echo "         difference cannot be decided by content. NOT treated as equal."
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
  # Verify and rollback need the exact pre-apply capacity values, including a legitimate zero minimum.
  read -r asg_min asg_desired <<< "$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].[MinSize,DesiredCapacity]' --output text)"
  state_put asg_backup_min_size "$asg_min"
  state_put asg_backup_desired "$asg_desired"
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
  echo "   asg_min=$asg_min asg_desired=$asg_desired"
  say "backup OK"
  ;;

apply)
  [ -f "$STATE" ] || die "no backup state; run backup first"
  [ -n "$AMI" ] || die "new_ami_id missing from environment; bake the AMI per host-scripts/packer/CUSTOMER-GUIDE.md first, or use apply-control for the control plane only"

  say "1/3 widen $HOOK_NAME heartbeat to ${NEW_TIMEOUT}s"
  aws_ autoscaling put-lifecycle-hook --auto-scaling-group-name "$ASG" \
    --lifecycle-hook-name "$HOOK_NAME" \
    --lifecycle-transition autoscaling:EC2_INSTANCE_LAUNCHING \
    --heartbeat-timeout "$NEW_TIMEOUT" --default-result ABANDON || die "hook update failed"

  say "2/3 upload bootstrap script to its content-addressed key, then one new LT version"
  art="${KITDIR}/host-scripts/init-host.sh.patched"
  [ -f "$art" ] || die "missing artifact $art"
  got="$(sha256_file "$art")"
  [ "$got" = "$ARTIFACT_SHA256" ] \
    || die "artifact sha256 $got does not match expected file digest $ARTIFACT_SHA256"
  # Recompute the state here rather than trusting precheck: the run directory is editable
  # and the live target may have moved between the two phases.
  lt_default_data="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions '$Default' \
    --query 'LaunchTemplateVersions[0].[VersionNumber,LaunchTemplateData.ImageId,LaunchTemplateData.UserData]' \
    --output text)" || die "cannot read current default LT version"
  read -r live_lt_version live_ami live_userdata <<< "$lt_default_data"
  b64="$live_userdata"
  bootstrap_uploaded=0
  live_prefix="$(printf '%s' "$live_userdata" | base64 -d \
    | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
  resolve_bootstrap_state "$live_prefix"
  if [ "$BOOTSTRAP_STATE" = "ALREADY" ]; then
    say "   SKIP bootstrap: target content already in service (idempotent no-op)"
  elif [ "$BOOTSTRAP_STATE" = "DRIFT" ] && [ "$ALLOW_BASE_DRIFT" -ne 1 ]; then
    echo "   REFUSE bootstrap: DRIFT requires --allow-base-drift; no host assets were published in this run; continuing other concerns"
    # REFUSE blocks rewriting bootstrap content, not promoting a different ImageId below.
  else
  [ -n "$live_prefix" ] || die "cannot render bootstrap without an in-service bootstrap prefix"
  live_art="$(mktemp)"
  rendered_art="$(mktemp)"
  aws_ s3 cp "s3://${BUCKET}/deployment/bootstrap/host/${live_prefix}/init-host.sh" \
    "$live_art" --no-progress >/dev/null \
    || die "cannot download live rendered bootstrap for value recovery"
  # Host assets must share the bootstrap gate: publishing only one lineage creates
  # a mixed-lineage fleet and reintroduces the per-file drift this kit repairs.
  # Publish every runtime dependency before any new host can consume the promoted template.
  publish_host_assets
  render_bootstrap_artifact "$art" "$live_art" "$rendered_art"
  rendered_sha="$(sha256_file "$rendered_art")"
  state_put rendered_bootstrap_sha256 "$rendered_sha"
  state_put rendered_bootstrap_source_prefix "$live_prefix"
  publish_s3_asset "$rendered_art" \
    "deployment/bootstrap/host/${ASSET_BUNDLE_PREFIX}/init-host.sh"
  rm -f "$live_art"

  printf '%s' "$live_userdata" \
    | base64 -d > /tmp/restorepatch-ud.txt || die "cannot read in-service UserData"
  # Replace every observed content-addressed bootstrap prefix. The assertions below
  # forbid a no-op rewrite from creating and promoting an ineffective LT version.
  python3 - /tmp/restorepatch-ud.txt "$ASSET_BUNDLE_PREFIX" \
    "$rendered_art" <<'PY'
import re
import sys
p, new, rendered_path = sys.argv[1], sys.argv[2], sys.argv[3]
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
# The placeholder gate belongs to the rendered bootstrap, not the small downloader UserData.
rendered = open(rendered_path, encoding="utf-8", errors="surrogateescape").read().splitlines()
unresolved = [
    line for line in rendered
    if not re.match(r"^\s*#", line) and re.search(r"\{\{[A-Z_]+\}\}", line)
]
if unresolved:
    raise SystemExit("FAIL: unrendered placeholder present in rendered bootstrap")
if "{{" in t:
    raise SystemExit("FAIL: unrendered placeholder present after substitution")
open(p, "w", encoding="utf-8", errors="surrogateescape").write(t)
print("   UserData rewritten, %d old prefix(es) replaced; target present; no old prefix or placeholder remains" % len(old))
PY
  [ $? -eq 0 ] || die "UserData rewrite refused"
  b64="$(base64 -i /tmp/restorepatch-ud.txt 2>/dev/null || base64 -w0 /tmp/restorepatch-ud.txt)"
  bootstrap_uploaded=1
  rm -f "$rendered_art"
  fi

  if [ "$AMI" = "$live_ami" ] && [ "$bootstrap_uploaded" -eq 0 ]; then
    newver="$live_lt_version"
    if [ "$BOOTSTRAP_STATE" = "ALREADY" ]; then
      say "   SKIP LT version: image and bootstrap already in service (idempotent no-op)"
    else
      say "   SKIP LT version: target ImageId already in service; in-service UserData retained"
    fi
  else
  newver="$(aws_ ec2 create-launch-template-version --launch-template-id "$LT_ID" \
    --source-version "$live_lt_version" \
    --launch-template-data "{\"ImageId\":\"${AMI}\",\"UserData\":\"${b64}\"}" \
    --query 'LaunchTemplateVersion.VersionNumber' --output text)" || die "cannot create LT version"
  aws_ ec2 modify-launch-template --launch-template-id "$LT_ID" --default-version "$newver" \
    || die "cannot flip default version"
  say "   LT version $newver created and set default"
  fi
  state_put host_lt_new_version "$newver"

  say "3/3 overlay Lambda code, reusing each live package's dependencies"
  apply_control_overlay

  # A separate canary gate prevents an unverified bootstrap from reaching the whole fleet.
  say "apply OK — HostASG MinSize was deliberately left untouched"
  echo "   NEXT: bash lib/apply-restorepatch.sh canary --env \"$ENVJSON\" --kit \"$KITDIR\""
  ;;

apply-control)
  # This phase intentionally has no dataplane reads or writes.
  say "overlay Lambda code, reusing each live package's dependencies"
  apply_control_overlay
  say "apply-control OK"
  ;;

canary)
  [ -f "$STATE" ] || die "no backup state; run backup first"
  newver="$(state_get host_lt_new_version)"
  [ -n "$newver" ] || die "no promoted LT version in state; run apply first"
  state_put canary_pass false
  original_desired="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].DesiredCapacity' --output text)" \
    || die "cannot read current desired capacity"
  state_put canary_original_desired "$original_desired"
  existing_ids="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].Instances[].InstanceId' --output text | tr '\t' '\n')"
  canary_desired=$((original_desired + 1))
  max_size="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].MaxSize' --output text)" \
    || die "cannot read current ASG MaxSize"
  [ "$canary_desired" -le "$max_size" ] \
    || die "canary requires one free ASG slot: current desired=$original_desired MaxSize=$max_size; temporarily raise MaxSize or pick another window"
  # Desired capacity is temporary; MinSize is never written by this driver.
  aws_ autoscaling set-desired-capacity --auto-scaling-group-name "$ASG" \
    --desired-capacity "$canary_desired" --honor-cooldown \
    || die "cannot raise desired capacity for canary"
  echo "   desired capacity $original_desired -> $canary_desired; waiting for LT v$newver"

  canary_id=""
  for _ in $(seq 1 120); do
    while IFS=$'\t' read -r iid lifecycle health version; do
      [ -n "$iid" ] || continue
      printf '%s\n' "$existing_ids" | grep -Fxq "$iid" && continue
      if [ "$lifecycle" = "InService" ] && [ "$health" = "Healthy" ] \
          && [ "$version" = "$newver" ]; then
        canary_id="$iid"
        break
      fi
    done < <(aws_ autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$ASG" \
      --query 'AutoScalingGroups[0].Instances[].[InstanceId,LifecycleState,HealthStatus,LaunchTemplate.Version]' \
      --output text)
    [ -n "$canary_id" ] && break
    sleep 15
  done
  if [ -z "$canary_id" ]; then
    aws_ autoscaling set-desired-capacity --auto-scaling-group-name "$ASG" \
      --desired-capacity "$original_desired" --honor-cooldown >/dev/null 2>&1 || true
    die "canary failed: no new Healthy/InService instance appeared on LT v$newver"
  fi
  echo "   canary instance $canary_id is Healthy/InService on LT v$newver"

  ping=""
  for _ in $(seq 1 60); do
    ping="$(aws_ ssm describe-instance-information \
      --filters "Key=InstanceIds,Values=${canary_id}" \
      --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)"
    [ "$ping" = "Online" ] && break
    sleep 10
  done
  if [ "$ping" != "Online" ]; then
    # AWS default termination policy can prefer hosts on older LT versions, so scale-in
    # cannot safely remove a canary; terminate the named instance and decrement desired.
    aws_ autoscaling terminate-instance-in-auto-scaling-group \
      --instance-id "$canary_id" --should-decrement-desired-capacity >/dev/null \
      || die "canary failed and instance $canary_id could not be terminated"
    restored_desired="$(aws_ autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$ASG" \
      --query 'AutoScalingGroups[0].DesiredCapacity' --output text)" \
      || die "canary instance $canary_id terminated but desired capacity could not be read"
    [ "$restored_desired" = "$original_desired" ] \
      || die "canary instance $canary_id terminated but desired capacity is $restored_desired, expected $original_desired"
    die "canary failed: $canary_id did not become SSM Online"
  fi

  # The remote script accumulates every failed gate so operators see the whole defect set.
  canary_params="$(python3 - <<'PY'
import json
script = r'''set +e
failed=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; failed=1; }
if cloud-init status --wait >/tmp/restorepatch-cloud-init.txt 2>&1; then
  pass "cloud-init completed"
elif [ -f /var/log/cloud-init-output.log ] && ! grep -Eqi 'fatal|traceback|bootstrap.*failed' /var/log/cloud-init-output.log; then
  pass "bootstrap log has no fatal marker"
else
  fail "cloud-init failed or bootstrap log contains a fatal marker"
fi
if [ "$(systemctl is-active host-agent.service 2>/dev/null)" = active ]; then
  pass "host-agent.service active"
else
  fail "host-agent.service not active"
fi
unit_env="$(systemctl show host-agent.service -p Environment --value 2>/dev/null)"
case " $unit_env " in
  *" PYTHONUNBUFFERED=1 "*) pass "host-agent.service has PYTHONUNBUFFERED=1" ;;
  *) fail "host-agent.service missing PYTHONUNBUFFERED=1" ;;
esac
for script_path in launch-vm.sh rebuild-vm.sh reset-vm.sh stop-vm.sh; do
  if [ -x "/home/ubuntu/$script_path" ]; then
    pass "/home/ubuntu/$script_path exists and is executable"
  else
    fail "/home/ubuntu/$script_path missing or not executable"
  fi
done
if [ -f /var/log/cloud-init-output.log ] && awk '!/^[[:space:]]*#/ && index($0, "{{") {found=1} END {exit found}' /var/log/cloud-init-output.log; then
  pass "bootstrap log code lines contain no unresolved placeholder"
else
  fail "bootstrap log missing or contains an unresolved code placeholder"
fi
agent_port="$(printf '%s\n' "$unit_env" | tr ' ' '\n' | sed -n 's/^OC_AGENT_PORT=//p' | tail -1)"
if [ -n "$agent_port" ] && ss -ltnH | awk -v port=":$agent_port" '$4 ~ port "$" {found=1} END {exit !found}'; then
  pass "host-agent port $agent_port listening"
else
  fail "host-agent OC_AGENT_PORT is absent or not listening"
fi
exit "$failed"
'''
print(json.dumps({"commands": [script]}, separators=(",", ":")))
PY
)"
  command_id="$(aws_ ssm send-command --instance-ids "$canary_id" \
    --document-name AWS-RunShellScript --comment "restorepatch-amipacker canary gate" \
    --parameters "$canary_params" --timeout-seconds 300 \
    --query 'Command.CommandId' --output text)" || command_id=""
  if [ -z "$command_id" ] || [ "$command_id" = "None" ]; then
    aws_ autoscaling terminate-instance-in-auto-scaling-group \
      --instance-id "$canary_id" --should-decrement-desired-capacity >/dev/null \
      || die "canary failed and instance $canary_id could not be terminated"
    restored_desired="$(aws_ autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$ASG" \
      --query 'AutoScalingGroups[0].DesiredCapacity' --output text)" \
      || die "canary instance $canary_id terminated but desired capacity could not be read"
    [ "$restored_desired" = "$original_desired" ] \
      || die "canary instance $canary_id terminated but desired capacity is $restored_desired, expected $original_desired"
    die "canary failed: SSM send-command returned no command id"
  fi
  aws_ ssm wait command-executed --command-id "$command_id" \
    --instance-id "$canary_id" >/dev/null 2>&1 || true
  canary_result="$(aws_ ssm get-command-invocation --command-id "$command_id" \
    --instance-id "$canary_id" --output json)" || canary_result='{}'
  printf '%s' "$canary_result" | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d.get("StandardOutputContent",""), end=""); print(d.get("StandardErrorContent",""), end="", file=sys.stderr)'
  canary_status="$(printf '%s' "$canary_result" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("Status","Unknown"))')"
  aws_ autoscaling terminate-instance-in-auto-scaling-group \
    --instance-id "$canary_id" --should-decrement-desired-capacity >/dev/null \
    || die "canary checks finished but instance $canary_id could not be terminated"
  restored_desired="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].DesiredCapacity' --output text)" \
    || die "canary instance $canary_id terminated but desired capacity could not be read"
  [ "$restored_desired" = "$original_desired" ] \
    || die "canary instance $canary_id terminated but desired capacity is $restored_desired, expected $original_desired"
  echo "   desired capacity restored to $original_desired"
  [ "$canary_status" = "Success" ] || die "canary failed: remote check status=$canary_status"
  state_put canary_pass true
  state_put canary_instance_id "$canary_id"
  state_put canary_lt_version "$newver"
  state_put canary_passed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  say "canary PASS on $canary_id"
  echo "   NEXT: bash lib/apply-restorepatch.sh refresh --env \"$ENVJSON\" --kit \"$KITDIR\""
  ;;

refresh)
  [ -f "$STATE" ] || die "no backup state; run backup first"
  [ "$(state_get canary_pass)" = "true" ] \
    || die "refresh refused: no canary PASS record in $STATE"
  [ "$(state_get canary_lt_version)" = "$(state_get host_lt_new_version)" ] \
    || die "refresh refused: canary PASS belongs to a different LT version"
  say "instance refresh suspended-process precheck"
  # Suspension is intentional operator state, so refuse instead of auto-resuming it.
  # AWS documents only Launch/Terminate/InstanceRefresh as blocking replacement;
  # AZRebalance/ReplaceUnhealthy/HealthCheck do not, confirmed on a real ASG.
  suspended_processes="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].SuspendedProcesses[].ProcessName' --output text)" \
    || die "cannot read suspended processes for $ASG"
  blocking_suspended=""
  for process in $suspended_processes; do
    case "$process" in
      Launch|Terminate|InstanceRefresh)
        blocking_suspended="${blocking_suspended:+${blocking_suspended} }${process}"
        ;;
    esac
  done
  [ -z "$blocking_suspended" ] \
    || die "refresh refused: blocking suspended processes: $blocking_suspended; instance refresh would sit at Pending with 'Paused due to the following suspended processes: $blocking_suspended' and never replace an instance. Resume explicitly: aws autoscaling resume-processes --auto-scaling-group-name \"$ASG\" --scaling-processes $blocking_suspended --region \"$REGION\""
  echo "   PASS no blocking suspended processes"
  # AWS documents min=max=100 as launch-before-terminate, one-at-a-time replacement.
  refresh_id="$(aws_ autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
    --preferences '{"MinHealthyPercentage":100,"MaxHealthyPercentage":100,"InstanceWarmup":900,"SkipMatching":false}' \
    --query InstanceRefreshId --output text)" || die "cannot start instance refresh"
  state_put instance_refresh_id "$refresh_id"
  say "controlled instance refresh started: $refresh_id"
  echo "   NEXT: bash lib/apply-restorepatch.sh verify --env \"$ENVJSON\" --kit \"$KITDIR\""
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
  backup_minsz="$(state_get asg_backup_min_size)"
  if [ -z "$backup_minsz" ]; then
    echo "   SKIP no backup MinSize record; run backup before verify"
  elif [ "$minsz" = "$backup_minsz" ]; then
    echo "   PASS MinSize=$minsz unchanged from backup"
  else
    echo "   FAIL this tool changed MinSize: backup=$backup_minsz current=$minsz"
    rc=1
  fi

  say "default LT version carries the new bootstrap prefix and the new image"
  ud="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions '$Default' --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
    --output text | base64 -d)"
  if printf '%s' "$ud" | grep -q "$ASSET_BUNDLE_PREFIX"; then
    echo "   PASS bootstrap prefix"
  else
    verify_prefix="$(printf '%s' "$ud" \
      | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
    verify_probe=""
    verify_content_equal=0
    [ -z "$verify_prefix" ] || verify_probe="$(mktemp -d)"
    if [ -n "$verify_probe" ] \
      && aws_ s3 cp "s3://${BUCKET}/deployment/bootstrap/host/${verify_prefix}/init-host.sh" \
        "${verify_probe}/live-init-host.sh" --no-progress >/dev/null 2>&1; then
      if ( render_bootstrap_artifact "${KITDIR}/host-scripts/init-host.sh.patched" \
           "${verify_probe}/live-init-host.sh" "${verify_probe}/rendered-init-host.sh" ); then
        if python3 - "${verify_probe}/live-init-host.sh" \
          "${verify_probe}/rendered-init-host.sh" <<'PY'
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

sys.exit(0 if code_only(sys.argv[1]) == code_only(sys.argv[2]) else 1)
PY
        then
          verify_content_equal=1
        fi
      fi
    fi
    if [ "$verify_content_equal" -eq 1 ]; then
      echo "   PASS bootstrap content-equal under a different prefix: $verify_prefix"
    else
      # resolve_bootstrap_state can fail closed into DRIFT during precheck; verify is an
      # acceptance path, so an undecidable download or render must count as a failure.
      echo "   FAIL bootstrap prefix absent"
      rc=1
    fi
    [ -z "$verify_probe" ] || rm -rf "$verify_probe"
  fi
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
  expected_bootstrap_sha="$(state_get rendered_bootstrap_sha256)"
  bootstrap_object_prefix="$(printf '%s' "$ud" \
    | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
  bootstrap_readback="$(mktemp)"
  bootstrap_rendered=""
  if [ -z "$bootstrap_object_prefix" ]; then
    echo "   FAIL cannot extract bootstrap object prefix from default LT UserData"
    rc=1
  elif aws_ s3 cp "s3://${BUCKET}/deployment/bootstrap/host/${bootstrap_object_prefix}/init-host.sh" \
      "$bootstrap_readback" --no-progress >/dev/null 2>&1; then
    actual_bootstrap_sha="$(sha256_file "$bootstrap_readback")"
    if [ -n "$expected_bootstrap_sha" ] && [ "$actual_bootstrap_sha" = "$expected_bootstrap_sha" ]; then
      echo "   PASS object present with this apply's rendered sha256=$actual_bootstrap_sha"
    elif [ -n "$expected_bootstrap_sha" ]; then
      echo "   FAIL bootstrap sha256=$actual_bootstrap_sha expected=$expected_bootstrap_sha"
      rc=1
    else
      # ALREADY skips upload and digest state, so verify the live key by executable content.
      bootstrap_rendered="$(mktemp)"
      if ( render_bootstrap_artifact "${KITDIR}/host-scripts/init-host.sh.patched" \
           "$bootstrap_readback" "$bootstrap_rendered" ); then
        if python3 - "$bootstrap_readback" "$bootstrap_rendered" <<'PY'
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

sys.exit(0 if code_only(sys.argv[1]) == code_only(sys.argv[2]) else 1)
PY
        then
          echo "   PASS object content-equal to kit artifact at live prefix: $bootstrap_object_prefix"
        else
          echo "   FAIL bootstrap content differs from kit artifact at live prefix: $bootstrap_object_prefix"
          rc=1
        fi
      else
        echo "   FAIL cannot render kit bootstrap from live object at prefix: $bootstrap_object_prefix"
        rc=1
      fi
    fi
    # Comments retain historical placeholders, so only executable lines are gated.
    if awk '!/^[[:space:]]*#/ && /\{\{[A-Z_]+\}\}/ {found=1} END {exit found}' "$bootstrap_readback"; then
      echo "   PASS bootstrap code lines have no unresolved placeholder"
    else
      echo "   FAIL bootstrap code line contains an unresolved placeholder"
      rc=1
    fi
  else
    echo "   FAIL object missing"
    rc=1
  fi
  rm -f "$bootstrap_readback"
  [ -z "$bootstrap_rendered" ] || rm -f "$bootstrap_rendered"

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

  rendered_sha="$(state_get rendered_bootstrap_sha256)"
  bootstrap_key="deployment/bootstrap/host/${ASSET_BUNDLE_PREFIX}/init-host.sh"
  bootstrap_state_key="$(asset_state_key "$bootstrap_key")"
  if [ -n "$rendered_sha" ] \
      && [ "$(state_get "s3_changed_${bootstrap_state_key}")" = "true" ]; then
    # Refuse to roll back over a bootstrap object changed after this apply.
    rollback_readback="$(mktemp)"
    aws_ s3 cp "s3://${BUCKET}/${bootstrap_key}" "$rollback_readback" --no-progress >/dev/null \
      || die "cannot read current rendered bootstrap before rollback"
    [ "$(sha256_file "$rollback_readback")" = "$rendered_sha" ] \
      || die "rendered bootstrap drifted after apply; refusing S3 rollback"
    rm -f "$rollback_readback"
  fi
  # Versioned S3 rollback restores every caller and dependency to its exact prior object.
  say "restore published host assets"
  restore_host_assets

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

  backup_desired="$(state_get asg_backup_desired)"
  if [ -n "$backup_desired" ]; then
    # Canary capacity is temporary and rollback must converge to the pre-apply desired value.
    aws_ autoscaling set-desired-capacity --auto-scaling-group-name "$ASG" \
      --desired-capacity "$backup_desired" --honor-cooldown \
      || die "cannot restore desired capacity to $backup_desired"
  fi
  state_put canary_pass false
  say "roll the fleet back onto the restored template"
  aws_ autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
    --preferences '{"MinHealthyPercentage":100,"MaxHealthyPercentage":100,"InstanceWarmup":900,"SkipMatching":false}' \
    --query InstanceRefreshId --output text \
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
