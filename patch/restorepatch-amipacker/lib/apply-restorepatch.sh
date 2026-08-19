#!/usr/bin/env bash
# apply-restorepatch.sh — deterministic driver for restorepatch-amipacker.
#
# The kit's synthesized CloudFormation resource changes are applied, verified and rolled
# back ONLY through this tool, so no operator hand-assembles a fleet-breaking command
# sequence and every executor runs identical code. The count is not repeated here: it is
# whatever `manifest.json` binds (44 at the time of writing, not the twelve this line
# used to claim), and a hardcoded number in a comment goes stale the first time the
# closure is re-captured.
#
# It covers exactly four concerns and refuses to do anything else:
#   1. host-init lifecycle hook heartbeat timeout 1200 -> 3600
#   2. one new LaunchTemplate version carrying the Packer AMI id AND the new
#      content-addressed bootstrap prefix, then default-version flip + controlled refresh
#   3. API-package function code overlay (reuses each live package's own dependencies)
#   4. private REST API endpoint attachment followed by deployment and stage replacement
#
# Deliberately NOT done, and it will refuse if asked:
#   * HostASG MinSize 2 -> 0. That value is first-deployment semantics; on a live fleet it
#     permits scaling to zero hosts that carry real tenants.
#   * TrackDefaultLTVersion AsgShape digest and the OpenClawImage CodeBuild churn. Both are
#     derived or content-hash projections with no independent action.
#
# usage: lib/apply-restorepatch.sh <precheck|reconcile|backup|apply|apply-control|canary|refresh|verify|rollback|apply-api|verify-api|finalize-api|rollback-api> --env <environment.json> --kit <kit-dir> [--values <values.json>] [--allow-base-drift] [--scope <control|data|routes|all>] [--reanchor]
set -uo pipefail

PHASE="${1:?phase required: precheck|reconcile|backup|apply|apply-control|canary|refresh|verify|rollback|apply-api|verify-api|finalize-api|rollback-api}"; shift || true
ENVJSON=""; KITDIR="."; VALUESJSON=""; ALLOW_BASE_DRIFT=0; VERIFY_SCOPE="all"; VERIFY_SCOPE_SET=0; REANCHOR=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENVJSON="${2:?}"; shift 2 ;;
    --kit) KITDIR="${2:?}"; shift 2 ;;
    --values) VALUESJSON="${2:?}"; shift 2 ;;
    --allow-base-drift) ALLOW_BASE_DRIFT=1; shift ;;
    --scope) VERIFY_SCOPE="${2:?}"; VERIFY_SCOPE_SET=1; shift 2 ;;
    --reanchor) REANCHOR=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$VERIFY_SCOPE" in
  control|data|routes|all) ;;
  *) echo "unknown verify scope: $VERIFY_SCOPE" >&2; exit 2 ;;
esac
case "$PHASE" in
  verify|verify-api) ;;
  reconcile)
    case "$VERIFY_SCOPE" in
      control|data|all) ;;
      *) echo "reconcile scope must be control, data, or all" >&2; exit 2 ;;
    esac
    ;;
  *) [ "$VERIFY_SCOPE_SET" -eq 0 ] || { echo "--scope is only valid for reconcile, verify, and verify-api" >&2; exit 2; } ;;
esac
[ "$REANCHOR" -eq 0 ] || [ "$PHASE" = "backup" ] \
  || { echo "--reanchor is only valid for backup" >&2; exit 2; }
[ -f "$ENVJSON" ] || { echo "FATAL: environment file not found: $ENVJSON" >&2; exit 2; }
[ -z "$VALUESJSON" ] || [ -f "$VALUESJSON" ] \
  || { echo "FATAL: values file not found: $VALUESJSON" >&2; exit 2; }

# These values identify CDK asset bundles and therefore name S3 prefixes. They are
# not hashes of init-host.sh, whose independent byte digest is checked separately.
BASE_ASSET_BUNDLE_PREFIX=dea0bd3d54ac88764319c07b1b546df952e8e828f7f3aba0d18866160ee2d046
ASSET_BUNDLE_PREFIX=f6e72b08706d1e01503d4cd738f4a7af1840bdcfedb3878d38b10a3123d1c1f2
HOOK_NAME=openclaw-host-init
NEW_TIMEOUT=3600
STATE="${KITDIR}/.restorepatch-state.json"
KITDIR_ABS="$(cd "$KITDIR" 2>/dev/null && pwd -P)" \
  || { echo "FATAL: kit directory not found: $KITDIR" >&2; exit 2; }
BACKUP_DIR="${KITDIR_ABS}/.restorepatch-backups"

jget() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$1" "$2"; }
jpath() {
  python3 - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    value = value.get(key, "") if isinstance(value, dict) else ""
print("" if value is None else value if not isinstance(value, (dict, list)) else json.dumps(value))
PY
}

REGION="$(jpath "$ENVJSON" region)"
ASG="$(jpath "$ENVJSON" asg.name)"
LT_ID="$(jpath "$ENVJSON" asg.lt_id)"
LT_VER="$(jpath "$ENVJSON" asg.lt_version_pinned)"
BUCKET="$(jpath "$ENVJSON" assets.bucket)"
FN="$(jpath "$ENVJSON" lambda_link.function)"
AMI="$(jget "$ENVJSON" new_ami_id)"
PEER_RECORDS="$(jpath "$ENVJSON" lambda_link.peers)"
[ -n "$PEER_RECORDS" ] || PEER_RECORDS="[]"
PEER_DISCOVERY_CONFIRMED="$(jpath "$ENVJSON" lambda_link.peer_discovery_confirmed)"
FN_ESM_QUALIFIER="$(jpath "$ENVJSON" lambda_link.esm_qualifier)"
PEER_FNS="$(printf '%s' "$PEER_RECORDS" | python3 -c '
import json, sys
for record in json.load(sys.stdin):
    if record.get("probe_paths_present") is True:
        print(record.get("function", ""))
')"
PEER_ESM_QUALIFIERS="$(printf '%s' "$PEER_RECORDS" | python3 -c '
import json, sys
for record in json.load(sys.stdin):
    if record.get("probe_paths_present") is True:
        print("%s\t%s" % (
            record.get("function", ""),
            record.get("esm_qualifier", ""),
        ))
')"

# Control-plane-only phases must remain usable before the dataplane AMI and ASG are ready.
case "$PHASE" in
  apply-control|finalize-api|rollback-api)
    REQUIRED_VARS="REGION FN"
    ;;
  apply-api|verify-api)
    # These two resolve the target VPC from the host ASG before their first API Gateway
    # call, so an empty ASG cannot be caught later — it surfaces mid-phase, after the
    # endpoint work has already started. Require it up front instead.
    REQUIRED_VARS="REGION FN ASG"
    ;;
  reconcile)
    case "$VERIFY_SCOPE" in
      control) REQUIRED_VARS="REGION FN" ;;
      data) REQUIRED_VARS="REGION ASG BUCKET" ;;
      all) REQUIRED_VARS="REGION FN ASG BUCKET" ;;
    esac
    ;;
  verify)
    case "$VERIFY_SCOPE" in
      control) REQUIRED_VARS="REGION FN" ;;
      data) REQUIRED_VARS="REGION ASG LT_ID LT_VER BUCKET" ;;
      routes) REQUIRED_VARS="REGION" ;;
      all) REQUIRED_VARS="REGION ASG LT_ID LT_VER BUCKET FN" ;;
    esac
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

OC_ALLOW_PINNED_ASG="${OC_ALLOW_PINNED_ASG:-0}"
# ASG_LT_REF is whatever the ASG literally references: the alias '$Default' / '$Latest', or a pinned
# version number. That distinction decides whether this tool's promote reaches the fleet at
# all. apply builds a new LT version and points '$Default' at it, so an ASG pinned to a number
# keeps launching the old version while every '$Default'-based check passes -- which is exactly how
# a data-plane run reports success without changing a single host.
ASG_LT_REF="$LT_VER"
asg_lt_is_pinned() {
  case "$ASG_LT_REF" in
    ''|'$Default'|'$Latest') return 1 ;;
    *[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}
# Called by every phase that promotes or judges launch-template content. It refuses instead of
# guessing: the two safe resolutions have different blast radius, and repointing a fleet's ASG
# is the operator's call, not this script's.
assert_asg_lt_promotable() {
  asg_lt_is_pinned || return 0
  local dflt
  dflt="$(aws_ ec2 describe-launch-templates --launch-template-ids "$LT_ID" \
    --query 'LaunchTemplates[0].DefaultVersionNumber' --output text 2>/dev/null)" || dflt="?"
  if [ "$OC_ALLOW_PINNED_ASG" -eq 1 ]; then
    echo "   NOTE ASG $ASG is pinned to LT version $ASG_LT_REF (default=$dflt);"
    echo "        continuing because OC_ALLOW_PINNED_ASG=1 -- this run judges version $ASG_LT_REF,"
    echo "        and promoting the default alias will NOT reach this ASG until you repoint it."
    return 0
  fi
  die "ASG $ASG references launch-template version $ASG_LT_REF, not an alias (default=$dflt).
  This tool promotes by pointing the default alias at a new version, which does not reach a
  pinned ASG: new hosts would keep launching version $ASG_LT_REF while every default-based
  check passes, so the data-plane concern would report success without changing the fleet.
  Resolve explicitly, then re-run:
    (a) repoint the ASG at the alias, or
    (b) set OC_ALLOW_PINNED_ASG=1 to judge version $ASG_LT_REF in place and repoint the ASG
        yourself afterwards -- the promote will not take effect on its own."
}

# Resolve every module the overlay imports against ONE live package. Factored out of the
# precheck because the overlay is applied to the primary function AND to every peer that
# shares the package: a peer whose live package lacks an imported module fails at import
# time on every invocation, and the queued lifecycle path runs in a peer. Checking only the
# primary function reports PASS while a peer would be dead on arrival.
# usage: overlay_imports_resolve_against <function-name> [already-unpacked-dir]
overlay_imports_resolve_against() {
  local fn="$1" dir="${2:-}" loc w own=0 rc
  if [ -z "$dir" ]; then
    loc="$(aws_ lambda get-function --function-name "$fn" --query 'Code.Location' --output text)" \
      || { echo "   CANNOT read the package location of $fn"; return 1; }
    w="$(mktemp -d)"; own=1
    curl -fsS -o "${w}/live.zip" "$loc" || { echo "   CANNOT download the package of $fn"; rm -rf "$w"; return 1; }
    (cd "$w" && unzip -oq live.zip) || { echo "   CANNOT unpack the package of $fn"; rm -rf "$w"; return 1; }
    dir="$w"
  fi
python3 - "$KITDIR" "$dir" <<'PY'
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
  rc=$?
  [ "$own" -eq 1 ] && rm -rf "$w"
  return $rc
}

scope_includes() {
  case "${VERIFY_SCOPE}:$1" in
    all:*|control:control|data:data|routes:routes) return 0 ;;
    *) return 1 ;;
  esac
}

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

backup_anchor_put() {
  local key="$1" value="$2" existing
  existing="$(state_get "$key")"
  if [ -n "$existing" ] && [ "$REANCHOR" -eq 0 ]; then
    echo "   PRESERVE existing anchor $key"
    return 0
  fi
  if [ -n "$existing" ]; then
    echo "   REANCHOR $key"
  fi
  state_put "$key" "$value"
}

sha256_file() {
  local path="$1" value
  value="$(shasum -a 256 "$path" 2>/dev/null | awk '{print $1}')"
  [ -n "$value" ] || value="$(sha256sum "$path" | awk '{print $1}')"
  # Both tools failing used to still return 0 with an empty value. Two unhashable files then
  # compared equal, which is a match verdict manufactured out of a tooling failure. Callers that
  # compare digests must be able to tell that apart, so refuse anything that is not a digest.
  case "$value" in
    *[!0-9a-f]*|'') return 1 ;;
  esac
  [ "${#value}" -eq 64 ] || return 1
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
  # Unique archive per invocation. A fixed /tmp path keyed only on the function name is shared by
  # every concurrent run and by every environment patched from this workstation: one process could
  # replace the archive between another's entry assertion and its upload, publishing one
  # environment's package into another's function. A private directory closes that window and also
  # means the archive can never be a stale one from an earlier run.
  zip_dir="$(mktemp -d)"
  zip="${zip_dir}/${function_name}-restorepatch.zip"
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
  # Normalize mtimes before packing. Lambda derives CodeSha256 from the zip BYTES, and the
  # cp above stamps every overlaid file with the current time, so re-applying byte-identical
  # code still produced a different package: measured on a real function, two consecutive
  # apply-control runs yielded zips of the same size with zero content differences and 21
  # differing mtimes, yet CodeSha256 changed, a new version was published and the alias
  # advanced. That is what silently invalidated the recorded rollback anchor between runs.
  # 1980-01-01 is the zip epoch floor and is what CDK normalizes to for the same reason.
  find "$work" -exec touch -t 198001010000 {} + 2>/dev/null || true
  # Delete the target archive first. $zip is a FIXED path keyed only on the function name, and
  # `zip` without -FS never removes members, so a reused archive silently keeps entries whose
  # files are gone from $work. That defeats the guarantee the mtime normalization above exists to
  # establish, and across two environments patched from one workstation it would upload the first
  # environment's modules into the second one's function.
  rm -f "$zip"
  (cd "$work" && zip -qrX "$zip" .) || die "cannot repack $function_name"
  # Nothing else in the kit ever compares the PUBLISHED package's entry list to what was
  # assembled: verify and reconcile only check kit-declared paths, so a stray entry would ship
  # unnoticed. Assert the archive contains exactly the work tree.
  if ! diff -q <(cd "$work" && find . -type f -print | sed 's|^\./||' | LC_ALL=C sort) \
               <(unzip -Z1 "$zip" | grep -v '/$' | LC_ALL=C sort) >/dev/null; then
    die "$function_name package entries do not match the assembled tree; refusing to publish"
  fi
  aws_ lambda update-function-code --function-name "$function_name" \
    --zip-file "fileb://${zip}" >/dev/null || die "update-function-code failed for $function_name"
  aws_ lambda wait function-updated --function-name "$function_name" \
    || die "$function_name did not settle"
  state_put "zip_${function_name}" "$zip"
  # The package is on Lambda now; the local archive has no further use and must not linger where a
  # later run could find it.
  rm -rf "$zip_dir"
}

backup_function() {
  local function_name="$1" sha_key path_key
  local existing_path existing_sha location zip
  sha_key="${2:-backup_sha_${function_name}}"
  path_key="backup_${function_name}"
  existing_path="$(state_get "$path_key")"
  existing_sha="$(state_get "$sha_key")"
  if [ "$REANCHOR" -eq 0 ] && [ -n "$existing_path" ]; then
    [ -f "$existing_path" ] \
      || die "existing anchor $path_key points to a missing package; use --reanchor to replace it"
    backup_anchor_put "$path_key" "$existing_path"
    if [ -n "$existing_sha" ]; then
      backup_anchor_put "$sha_key" "$existing_sha"
    else
      backup_anchor_put "$sha_key" "$(sha256_file "$existing_path")"
    fi
    return
  fi
  [ "$REANCHOR" -eq 1 ] || [ -z "$existing_sha" ] \
    || die "existing anchor $sha_key has no package path; use --reanchor to replace it"
  mkdir -p "$BACKUP_DIR" || die "cannot create backup directory $BACKUP_DIR"
  location="$(aws_ lambda get-function --function-name "$function_name" \
    --query 'Code.Location' --output text)" || die "cannot locate $function_name package"
  zip="${BACKUP_DIR}/${function_name}-restorepatch-backup.zip"
  curl -fsS -o "$zip" "$location" || die "cannot back up $function_name"
  backup_anchor_put "$path_key" "$zip"
  backup_anchor_put "$sha_key" "$(sha256_file "$zip")"
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
template_text = pathlib.Path(template_path).read_text(
    encoding="utf-8", errors="surrogateescape")
whole_line_names = {
    "AGENTCORE_GATEWAY_URL", "FB_STREAM_HOST", "FB_STREAM_VM",
    "HOST_RESERVED_MEM", "HOST_RESERVED_VCPU", "NOMINAL_SPECS", "ROOTFS_PREFIX",
}
block_names = {
    "PROVISION_SCRIPT", "HOST_AGENT_SCRIPT", "BACKUP_DATA_SCRIPT", "HOST_USER_HOOK",
}
template_lines = template_text.splitlines(keepends=True)
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

names = sorted({
    match.group(1)
    for line in template_lines if not is_comment(line)
    for match in re.finditer(r"\{\{([A-Z0-9_]+)\}\}", line)
})

def without_newline(line):
    return line.rstrip("\r\n")

def has_placeholder(line):
    return re.search(r"\{\{[A-Z0-9_]+\}\}", line) is not None

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
        if name == "IMMUTABLE_DISK_REQUIRED":
            rendered = [
                line if is_comment(line) else line.replace(token, "false")
                for line in rendered
            ]
            print(
                "   bootstrap fallback: IMMUTABLE_DISK_REQUIRED=false "
                "(CDK/config compatibility default; no live recovery or --values entry)"
            )
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
    for match in re.finditer(r"\{\{([A-Z0-9_]+)\}\}", line)
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
  # GNU coreutils `stat -f` means "show FILESYSTEM status", not "format". It prints the
  # filesystem block to stdout and then exits non-zero, so with BSD-first probing the
  # command substitution captures that block AND the fallback's output, yielding a
  # multi-line $mode that can never equal "644". Probe GNU first; keep BSD as fallback.
  mode="$(stat -c '%a' "$local_path" 2>/dev/null || stat -f '%Lp' "$local_path")"
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
  # delete-vm.sh is a NEW hard dependency of the patched bootstrap: the patched
  # init-host.sh fetches it WITHOUT `|| true`, so an absent object makes every new host
  # burn the 20x15s retry budget and then exit 1 -> lifecycle ABANDON -> the ASG relaunches
  # -> unbounded churn, and `canary` can never pass. stop-vm.sh and backup-data.sh are
  # shipped patched too; publishing only some of them is exactly the mixed-lineage fleet
  # this function's own comment sets out to prevent.
  publish_s3_asset "$root/delete-vm.sh.patched" "deployment/scripts/delete-vm.sh"
  publish_s3_asset "$root/stop-vm.sh.patched" "deployment/scripts/stop-vm.sh"
  publish_s3_asset "$root/backup-data.sh.patched" "deployment/scripts/backup-data.sh"
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
  # The caller already downloaded the in-service bootstrap for value recovery; pass it so the gate
  # reads this environment's resolved LOGGING_ENABLED instead of re-fetching or assuming.
  assert_bootstrap_script_deps "$root" "${1:-}"
}

# Every `deployment/scripts/*` object the patched bootstrap fetches must exist in the bucket
# before a new host can consume the promoted template. Hand-maintaining the publish list
# above is what let delete-vm.sh slip through, so derive the requirement from the artifact
# that actually consumes it and fail loudly on any gap instead of discovering it as an
# ABANDON loop on real hardware.
# Recorded digests for one bare object name. Matching is by basename: the S3 key is
# `deployment/scripts/<name>` while the kit artifact is `host-scripts/<name>.patched`.
_dep_digests() {
  # Always two tokens, and a non-zero exit if the manifest cannot be read. Printing nothing on a
  # failure was indistinguishable from "no digest recorded", which downgraded the object to a
  # presence check -- so a pre-patch object passed the gate because a JSON read had failed.
  python3 - "${KITDIR}/manifest.json" "$1" <<'PY'
import json, sys
paths = json.load(open(sys.argv[1], encoding="utf-8")).get("paths") or {}
for r in paths.values():
    art = r.get("artifact") or ""
    if art.rsplit("/", 1)[-1] in (sys.argv[2], sys.argv[2] + ".patched"):
        print(r.get("base_sha256") or "-", r.get("patch_sha256") or "-")
        break
else:
    print("-", "-")
PY
}

# Digests recorded for one exact manifest path. `jpath` splits its key on '.', so it silently
# returns empty for any key containing a dot -- the shape of every file path -- which is why this
# reads the literal key instead.
_path_digests() {
  python3 - "${KITDIR}/manifest.json" "$1" <<'PY'
import json, sys
paths = json.load(open(sys.argv[1], encoding="utf-8")).get("paths") or {}
record = paths.get(sys.argv[2]) or {}
print(record.get("base_sha256") or "-", record.get("patch_sha256") or "-")
PY
}

# The in-service rendered bootstrap, which is the only authority on what this fleet actually boots
# with -- notably whether the Fluent Bit step runs at all. Callers that already hold a copy pass it
# in; the rest resolve it from the launch template the ASG references, and ONLY from there. This run's
# own recorded rendering was an obvious-looking fallback and a wrong one: it is not bound to this
# ASG, it can predate a repoint, and an older copy that said LOGGING_ENABLED=false would talk the
# gate out of checking a fleet that has logging on. Returning non-zero makes the caller assume
# logging is enabled, which is the safe direction.
_live_bootstrap_copy() {
  local out="$1" userdata prefix=""
  userdata="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions "$ASG_LT_REF" --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
    --output text 2>/dev/null)" || return 1
  case "${userdata:-}" in ''|None) return 1 ;; esac
  prefix="$(printf '%s' "$userdata" | base64 -d 2>/dev/null \
    | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
  [ -n "$prefix" ] || return 1
  aws_ s3 cp "s3://${BUCKET}/deployment/bootstrap/host/${prefix}/init-host.sh" "$out" \
    --no-progress >/dev/null 2>&1 || return 1
  [ -s "$out" ] || return 1
}

# Names under deployment/scripts/ that the patched bootstrap fetches, including the ones it builds
# from a loop variable. Fail closed if a variable-built key cannot be resolved: an unenumerable
# dependency is the same blind spot as an unchecked prefix.
_bootstrap_script_names() {
  local root="$1" out="$2" src="$1/init-host.sh.patched" words word rc
  # Writes to a file instead of stdout, and checks every producer. Behind a pipeline a grep that
  # dies after partial output, or a grep that cannot read the file at all, is indistinguishable from
  # "nothing to report" -- which would hand the caller a truncated requirement list and a PASS.
  # grep exits 1 for "no match" and >1 for a real error; only the first is an answer.
  [ -r "$src" ] || return 1
  : > "$out" || return 1
  grep -oE 'deployment/scripts/[A-Za-z0-9._/-]+' "$src" > "${out}.raw"
  rc=$?
  [ "$rc" -le 1 ] || return 1
  sed 's|deployment/scripts/||' "${out}.raw" >> "$out" || return 1
  grep -q 'deployment/scripts/\${' "$src"
  rc=$?
  case "$rc" in
    0) ;;
    1) return 0 ;;
    *) return 1 ;;
  esac
  words="$(sed -nE 's/^[[:space:]]*for[[:space:]]+_s[[:space:]]+in[[:space:]]+([^;]+);.*/\1/p' \
    "$src")" || return 1
  [ -n "$words" ] \
    || die "the patched bootstrap builds deployment/scripts keys from a variable this gate cannot resolve; extend _bootstrap_script_names before shipping, because an unenumerable boot dependency is how a missing object reaches real hardware as an ABANDON loop"
  for word in $words; do
    # A word list that is itself built from a variable resolves to nothing usable. Say that, rather
    # than emitting a nonsense key whose MISSING line would send an operator looking for an object
    # that was never meant to exist.
    case "$word" in
      *[\$\{\*\'\"]*)
        die "the patched bootstrap builds deployment/scripts keys from '$word', which this gate cannot resolve to object names; extend _bootstrap_script_names before shipping"
        ;;
    esac
    printf '%s.sh\n' "$word" >> "$out" || return 1
  done
}

assert_bootstrap_script_deps() {
  local root="$1" live_bootstrap="${2:-}" rel missing=0 stale=0 undecided=0 ok_n=0 presence_only=0
  local work base_sha patch_sha got dep_line
  local obs_ok=0 obs_note=0 obs_key obs_kit obs_want obs_copy obs_ref _obs_verdict
  local uncovered="" asset_key
  local names_file logging_enabled=true logging_source logging_count logging_value
  work="$(mktemp -d)"
  names_file="${work}/bootstrap-script-names"
  _bootstrap_script_names "$root" "$names_file" \
    || die "cannot enumerate the deployment/scripts objects the patched bootstrap fetches"
  [ -s "$names_file" ] \
    || die "the patched bootstrap names no deployment/scripts object; that cannot be right, so treat it as a broken derivation rather than an empty requirement"
  sort -u "$names_file" > "${names_file}.sorted" \
    || die "cannot sort the bootstrap script requirement list"
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    if ! aws_ s3api head-object --bucket "$BUCKET" --key "deployment/scripts/$rel" >/dev/null 2>&1; then
      echo "   MISSING s3://${BUCKET}/deployment/scripts/${rel}" >&2
      missing=1
      continue
    fi
    if ! dep_line="$(_dep_digests "${rel##*/}")"; then
      echo "   UNDECIDED ${rel} — the manifest digests could not be read, so nothing about this object is decided" >&2
      undecided=$((undecided + 1))
      continue
    fi
    case "$dep_line" in
      *' '*) base_sha="${dep_line%% *}"; patch_sha="${dep_line##* }" ;;
      *)
        echo "   UNDECIDED ${rel} — the manifest digest lookup returned '${dep_line}', not two digests" >&2
        undecided=$((undecided + 1))
        continue
        ;;
    esac
    if [ "${patch_sha:--}" = "-" ]; then
      presence_only=$((presence_only + 1))
      continue
    fi
    if ! aws_ s3 cp "s3://${BUCKET}/deployment/scripts/${rel}" "${work}/${rel##*/}" \
         --no-progress >/dev/null 2>&1; then
      echo "   UNREADABLE s3://${BUCKET}/deployment/scripts/${rel}" >&2
      undecided=$((undecided + 1))
      continue
    fi
    got="$(sha256_file "${work}/${rel##*/}")"
    if [ "$got" = "$patch_sha" ]; then
      ok_n=$((ok_n + 1))
    elif [ "$got" = "$base_sha" ]; then
      echo "   STALE ${rel} is still the pre-patch version — present but incompatible" >&2
      stale=$((stale + 1))
    else
      echo "   UNDECIDED ${rel} matches neither recorded digest (sha=${got})" >&2
      undecided=$((undecided + 1))
    fi
  done < "${names_file}.sorted"

  # The observability prefix is the SECOND set of objects this bootstrap consumes, and it was
  # outside the loop above. step3a2 fetches deployment/observability/fluent-bit/install-fluent-bit.sh
  # with no `|| true` and no baked fallback for role=host, then runs it; a pre-patch installer dies
  # there (its `gpg --dearmor` has no --batch, so on a boot with no terminal it fails to open
  # /dev/tty), init exits rc=2 and the lifecycle hook ABANDONs the instance. The installer then
  # pulls the whole role prefix and dies on a config whose Lua filters are absent, so the conf and
  # the .lua files are boot-critical too. A fleet lost every new host to exactly this on
  # 2026-08-19 after a partial publish left the pre-patch installer in place -- and the gate above
  # reported PASS, because it can only see deployment/scripts/.
  #
  # Scope: this bootstrap fetches the installer and, through it, the host role prefix -- and only
  # when the rendered LOGGING_ENABLED is true. The edge role is consumed by edge instances, not by
  # this template, so it is reported and not required; demanding it would refuse an environment
  # whose edge is managed by a deployment.
  if [ -n "$live_bootstrap" ] && [ -s "$live_bootstrap" ]; then
    logging_source="the in-service bootstrap"
  else
    live_bootstrap="${work}/live-init-host.sh"
    if _live_bootstrap_copy "$live_bootstrap"; then
      logging_source="the in-service bootstrap"
    else
      live_bootstrap=""
      logging_source="assumed enabled: no in-service bootstrap was reachable to read it from"
    fi
  fi
  if [ -n "$live_bootstrap" ]; then
    # Exactly one top-level assignment decides this, and anything else assumes enabled. Matching any
    # line meant a second assignment -- a later edit, or a line inside a heredoc that happens to
    # start at column 0 -- could override the effective value and skip every check below. A switch
    # that can be talked down by an unrelated line is not a switch.
    logging_count="$(grep -cE '^LOGGING_ENABLED=' "$live_bootstrap")" || logging_count=0
    if [ "$logging_count" = "1" ]; then
      logging_value="$(sed -nE 's/^LOGGING_ENABLED="?([A-Za-z]+)"?[[:space:]]*$/\1/p' "$live_bootstrap")"
      case "$logging_value" in
        false) logging_enabled=false ;;
        true) ;;
        *) logging_source="assumed enabled: the in-service bootstrap sets LOGGING_ENABLED to '${logging_value}', which is not a boolean" ;;
      esac
    else
      logging_source="assumed enabled: the in-service bootstrap carries ${logging_count} top-level LOGGING_ENABLED assignments, so none of them is authoritative"
    fi
  fi
  if [ "$logging_enabled" != true ]; then
    echo "   SKIP the Fluent Bit objects: ${logging_source} sets LOGGING_ENABLED=false, so step3a2 never runs"
  else
    echo "   logging is enabled according to ${logging_source}; the Fluent Bit objects are boot-critical"
    # Every derived list goes to a file whose producer status is checked. Behind a process
    # substitution a failing find/sed/grep is indistinguishable from an empty result, and an empty
    # result here would print PASS over zero objects -- a green light produced by a broken pipe.
    ( cd "$root/edge/fluent-bit" && find . -type f -print ) 2>/dev/null \
      | sed 's|^\./||' | sort > "${work}/obs-files" \
      || die "cannot enumerate the kit's Fluent Bit artifacts"
    [ -s "${work}/obs-files" ] \
      || die "the kit ships no Fluent Bit artifacts under host-scripts/edge/fluent-bit; refusing to report a converged observability prefix from an empty list"
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      case "$rel" in
        __pycache__/*|*/__pycache__/*|*.pyc|*.pyo|.DS_Store|*/.DS_Store) continue ;;
      esac
      obs_kit="$root/edge/fluent-bit/$rel"
      obs_key="deployment/observability/fluent-bit/$rel"
      if ! obs_want="$(sha256_file "$obs_kit")"; then
        die "cannot hash the kit's own $obs_kit; refusing to compare against a digest this run could not compute"
      fi
      obs_copy="${work}/obs-$(printf '%s' "$rel" | tr '/' '_')"
      if ! aws_ s3 cp "s3://${BUCKET}/${obs_key}" "$obs_copy" --no-progress >/dev/null 2>&1; then
        # Absent, forbidden and unreadable are three different verdicts, and only the first is a gap
        # this driver can close by publishing. HeadObject's failure text is the only thing that
        # separates them, so read it instead of assuming absence.
        if aws_ s3api head-object --bucket "$BUCKET" --key "$obs_key" >"${work}/head-out" 2>"${work}/head-err"; then
          _obs_verdict="UNREADABLE (present, but this run could not download it)"
        elif grep -qE '\(404\)|Not Found|NoSuchKey' "${work}/head-err"; then
          _obs_verdict="MISSING"
        else
          _obs_verdict="UNDECIDED ($(tr '\n' ' ' < "${work}/head-err" | tail -c 160))"
        fi
        case "$rel" in
          edge/*)
            echo "   NOTE s3://${BUCKET}/${obs_key}: ${_obs_verdict} — edge role, not consumed by this bootstrap"
            obs_note=$((obs_note + 1))
            ;;
          *)
            echo "   ${_obs_verdict} s3://${BUCKET}/${obs_key}" >&2
            case "$_obs_verdict" in
              MISSING) missing=1 ;;
              *) undecided=$((undecided + 1)) ;;
            esac
            ;;
        esac
        continue
      fi
      if ! got="$(sha256_file "$obs_copy")"; then
        echo "   UNDECIDED ${obs_key} — the downloaded object could not be hashed" >&2
        undecided=$((undecided + 1))
        continue
      fi
      if [ "$got" = "$obs_want" ]; then
        case "$rel" in
          edge/*) obs_note=$((obs_note + 1)) ;;
          *) obs_ok=$((obs_ok + 1)) ;;
        esac
        continue
      fi
      if ! dep_line="$(_path_digests "deploy/edge/fluent-bit/${rel}")"; then
        dep_line="- -"
      fi
      base_sha="${dep_line%% *}"
      case "$rel" in
        edge/*)
          echo "   NOTE s3://${BUCKET}/${obs_key} differs from the kit copy (sha=${got}) — edge role, not consumed by this bootstrap"
          obs_note=$((obs_note + 1))
          ;;
        *)
          if [ "$got" = "$base_sha" ]; then
            echo "   STALE ${obs_key} is still the pre-patch version — present but incompatible" >&2
            stale=$((stale + 1))
          else
            echo "   UNDECIDED ${obs_key} matches neither the kit copy nor the recorded pre-patch digest (sha=${got})" >&2
            undecided=$((undecided + 1))
          fi
          ;;
      esac
    done < "${work}/obs-files"

    # Support files the shipped host config references but this kit does not carry. Presence is all
    # that can be judged (no digest exists for them), and presence is what the installer needs: it
    # dies on a missing Lua filter by name, and `fluent-bit --dry-run` dies on a missing
    # Parsers_File.
    if [ -f "$root/edge/fluent-bit/host/fluent-bit.conf" ]; then
      sed -nE \
        -e 's/^[[:space:]]*script[[:space:]]+([^[:space:]/][^[:space:]]*)[[:space:]]*$/\1/p' \
        -e 's/^[[:space:]]*Parsers_File[[:space:]]+([^[:space:]]+)[[:space:]]*$/\1/p' \
        "$root/edge/fluent-bit/host/fluent-bit.conf" | sort -u > "${work}/obs-refs" \
        || die "cannot read the support files the host Fluent Bit config references"
      [ -s "${work}/obs-refs" ] \
        || die "the host Fluent Bit config this kit publishes names no parser or Lua filter; that contradicts the config itself, so treat it as a broken derivation"
      while IFS= read -r obs_ref; do
        [ -n "$obs_ref" ] || continue
        # Anything the kit ships was already compared by digest in the loop above.
        [ -f "$root/edge/fluent-bit/host/$obs_ref" ] && continue
        if aws_ s3api head-object --bucket "$BUCKET" \
             --key "deployment/observability/fluent-bit/host/${obs_ref}" >/dev/null 2>"${work}/head-err"; then
          presence_only=$((presence_only + 1))
        elif grep -qE '\(404\)|Not Found|NoSuchKey' "${work}/head-err"; then
          echo "   MISSING s3://${BUCKET}/deployment/observability/fluent-bit/host/${obs_ref} (referenced by the host config this kit publishes; the installer dies on it)" >&2
          missing=1
        else
          echo "   UNDECIDED s3://${BUCKET}/deployment/observability/fluent-bit/host/${obs_ref} — cannot tell whether it exists ($(tr '\n' ' ' < "${work}/head-err" | tail -c 160))" >&2
          undecided=$((undecided + 1))
        fi
      done < "${work}/obs-refs"
    fi
  fi

  # Coverage, so this gate cannot go blind again. Every literal assets-bucket key the patched
  # bootstrap fetches must be claimed by one of the checks above; anything else stops the run and
  # names itself, instead of being quietly unchecked the way the observability prefix was. Keys
  # built from shell variables or {{placeholders}} do not survive the character class below, and
  # the two that are environment data rather than patch artifacts are exempt by name.
  grep -oE 's3://\$\{ASSETS_BUCKET\}/[A-Za-z0-9._/-]+' "$root/init-host.sh.patched" \
    | sed 's|^s3://\${ASSETS_BUCKET}/||' | sort -u > "${work}/asset-keys" \
    || die "cannot read the assets-bucket keys the patched bootstrap fetches"
  [ -s "${work}/asset-keys" ] \
    || die "the patched bootstrap appears to fetch nothing from the assets bucket; that contradicts the artifact, so treat it as a broken derivation rather than full coverage"
  while IFS= read -r asset_key; do
    [ -n "$asset_key" ] || continue
    case "$asset_key" in
      deployment/scripts/*) continue ;;
      deployment/observability/fluent-bit/*) continue ;;
      # The ADOT config is exempt for a specific reason, not by convenience: the kit ships no ADOT
      # bytes to compare against, and the bootstrap treats a failed fetch of this key as
      # recoverable -- it falls back to deployment/scripts/adot-config.yaml, which the loop above
      # does check, and only the fallback failing ends the boot. A present-but-stale config here
      # degrades metrics rather than the boot, so it is out of this gate's remit; verifying it
      # would need a content source this kit does not have.
      deployment/observability/adot/adot-config.yaml) continue ;;
      # Tenant skill content, synced with `|| true` and owned by nothing in this kit.
      skills/) continue ;;
      *) uncovered="${uncovered:+${uncovered} }${asset_key}" ;;
    esac
  done < "${work}/asset-keys"

  rm -rf "$work"
  [ -z "$uncovered" ] \
    || die "the patched bootstrap fetches assets-bucket object(s) no check in this gate covers: ${uncovered}; extend assert_bootstrap_script_deps before shipping, because an unchecked boot dependency is how a stale object reaches real hardware as an ABANDON loop"
  [ "$missing" -eq 0 ] \
    || die "the patched bootstrap fetches objects that are absent from the bucket (see MISSING above); publishing the promoted template now would ABANDON every new host"
  # Presence was never the question: all three canary ABANDONs on the customer run were
  # present-but-incompatible objects, which a head-object can never see.
  [ "$stale" -eq 0 ] \
    || die "$stale object(s) are still the pre-patch version (see STALE above); promoting now would ABANDON every new host for a reason a presence check cannot see"
  [ "$undecided" -eq 0 ] \
    || die "$undecided object(s) match neither recorded digest (see UNDECIDED/UNREADABLE above); refusing to promote against an undecided bucket"
  echo "   PASS ${ok_n} deployment/scripts object(s) match patch_sha256; ${presence_only} present-only (no digest recorded); ${obs_ok} host observability object(s) match the kit copy; ${obs_note} edge object(s) reported only"
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
  local root="${KITDIR}/host-scripts" rel eff
  # Restore every object this driver may have changed, including the rendered bootstrap.
  # The bootstrap this run published is keyed on THIS environment's rendering (see the
  # content-addressing note in the apply path), so restore that key too. restore_s3_asset
  # already no-ops for any key this run did not change, which keeps the manifest-prefix
  # call below correct for state files written before that change.
  eff="$(state_get rendered_bootstrap_sha256)"
  [ -n "$eff" ] && restore_s3_asset "deployment/bootstrap/host/${eff}/init-host.sh"
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

validate_control_overlay_scope() {
  local peer_fn peer_qualifier
  case "$PEER_DISCOVERY_CONFIRMED" in
    True|true) ;;
    *)
      die "API-package peer discovery is unconfirmed; rerun discover-env.sh from a host that can enumerate Lambda"
      ;;
  esac
  [ -z "$FN_ESM_QUALIFIER" ] \
    || die "$FN event source consumes pinned qualifier '$FN_ESM_QUALIFIER'; the \$LATEST overlay would not reach it"
  while IFS=$'\t' read -r peer_fn peer_qualifier; do
    [ -n "$peer_fn" ] || continue
    [ -z "$peer_qualifier" ] \
      || die "$peer_fn event source consumes pinned qualifier '$peer_qualifier'; the \$LATEST overlay would not reach it"
  done <<< "$PEER_ESM_QUALIFIERS"
}

apply_control_overlay() {
  # Keep the Lambda-only path independent from every dataplane coordinate.
  validate_control_overlay_scope
  overlay_function "$FN" "${KITDIR}/lambda/api"
  echo "   OVERLAID $FN (API package)"
  for peer_fn in $PEER_FNS; do
    overlay_function "$peer_fn" "${KITDIR}/lambda/api"
    echo "   OVERLAID $peer_fn (discovered API-package peer)"
  done
  overlay_function openclaw-backup "${KITDIR}/lambda/backup"
  echo "   OVERLAID openclaw-backup"
  overlay_function openclaw-health-check "${KITDIR}/lambda/health_check"
  echo "   OVERLAID openclaw-health-check"
  overlay_function openclaw-scaler "${KITDIR}/lambda/scaler"
  echo "   OVERLAID openclaw-scaler"
  if aws_ lambda get-function --function-name openclaw-tenant-stats-writer >/dev/null 2>&1; then
    overlay_function openclaw-tenant-stats-writer "${KITDIR}/lambda/tenant_stats"
    echo "   OVERLAID openclaw-tenant-stats-writer"
  else
    echo "   ABSENT openclaw-tenant-stats-writer; no deployed target for its module"
  fi
  if aws_ lambda get-function --function-name openclaw-console-bff >/dev/null 2>&1; then
    overlay_function openclaw-console-bff "${KITDIR}/lambda/console-bff"
    echo "   OVERLAID openclaw-console-bff"
  else
    echo "   ABSENT openclaw-console-bff; console delivery path is not deployed"
  fi
  # A re-run that changed nothing must not publish a version or move the alias -- that is exactly
  # what invalidated the recorded rollback anchor between runs, because a later `backup` then
  # captured the already-patched version as the rollback target. The package is now byte-
  # reproducible, so $LATEST hashes identically across runs; compare the code the alias actually
  # serves to $LATEST and skip when they already match. This check cannot be delegated to AWS:
  # PublishVersion mints a new version number even when the code is unchanged.
  latest_code_sha="$(aws_ lambda get-function-configuration --function-name "$FN" \
    --query CodeSha256 --output text)" || die "cannot read \$LATEST CodeSha256 for $FN"
  served_code_sha=""
  served_version=""
  if alias_for_skip="$(alias_name)"; then
    served_version="$(aws_ lambda get-alias --function-name "$FN" --name "$alias_for_skip" \
      --query FunctionVersion --output text 2>/dev/null)" || served_version=""
    case "$served_version" in
      ''|'$LATEST'|None) served_version="" ;;
      *)
        served_code_sha="$(aws_ lambda get-function-configuration --function-name "$FN" \
          --qualifier "$served_version" --query CodeSha256 --output text 2>/dev/null)" \
          || served_code_sha=""
        ;;
    esac
  fi
  if [ -n "$served_code_sha" ] && [ "$served_code_sha" = "$latest_code_sha" ]; then
    echo "   SKIP publish: alias $alias_for_skip already serves this exact code (version $served_version)"
    state_put api_applied_version "$served_version"
    state_put api_alias_name "$alias_for_skip"
    return 0
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
  # An already-applied environment carries the hash of ITS OWN rendering, which is only
  # equal to ASSET_BUNDLE_PREFIX when this environment renders byte-identically to the
  # author's. Treat the recorded rendered hash as the effective target so a rerun is still
  # the documented no-op.
  if [ "$live_prefix" = "$ASSET_BUNDLE_PREFIX" ] \
     || { [ -n "$(state_get rendered_bootstrap_sha256)" ] \
          && [ "$live_prefix" = "$(state_get rendered_bootstrap_sha256)" ]; }; then
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
    # NOTE: this same judgement is implemented at four sites in this file
    # (resolve_bootstrap_state, reconcile_b_s3, and twice in verify). Change them
    # together — a divergent copy makes two gates disagree about one environment.
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

reconcile_reset_counts() {
  RECON_PATCH=0
  RECON_BASE=0
  RECON_UNKNOWN=0
  RECON_ABSENT=0
  RECON_NOT_APPLICABLE=0
  RECON_UNMAPPED=0
  RECON_UNREADABLE=0
  RECON_SPLIT=0
  RECON_C_PATCH=0
  RECON_C_BASE=0
  RECON_C_UNKNOWN=0
  RECON_C_ABSENT=0
  RECON_C_NOT_APPLICABLE=0
  RECON_C_UNMAPPED=0
  RECON_C_UNREADABLE=0
  RECON_B_PATCH=0
  RECON_B_BASE=0
  RECON_B_UNKNOWN=0
  RECON_B_ABSENT=0
  RECON_B_NOT_APPLICABLE=0
  RECON_B_UNMAPPED=0
  RECON_B_UNREADABLE=0
}

reconcile_count_state() {
  local layer="$1" state="$2"
  case "$state" in
    PATCH) RECON_PATCH=$((RECON_PATCH + 1)) ;;
    BASE) RECON_BASE=$((RECON_BASE + 1)) ;;
    UNKNOWN) RECON_UNKNOWN=$((RECON_UNKNOWN + 1)) ;;
    ABSENT) RECON_ABSENT=$((RECON_ABSENT + 1)) ;;
    NOT_APPLICABLE) RECON_NOT_APPLICABLE=$((RECON_NOT_APPLICABLE + 1)) ;;
    UNMAPPED) RECON_UNMAPPED=$((RECON_UNMAPPED + 1)) ;;
    UNREADABLE) RECON_UNREADABLE=$((RECON_UNREADABLE + 1)) ;;
    *) die "internal reconcile state is invalid: $state" ;;
  esac
  case "${layer}:${state}" in
    C-lambda:PATCH) RECON_C_PATCH=$((RECON_C_PATCH + 1)) ;;
    C-lambda:BASE) RECON_C_BASE=$((RECON_C_BASE + 1)) ;;
    C-lambda:UNKNOWN) RECON_C_UNKNOWN=$((RECON_C_UNKNOWN + 1)) ;;
    C-lambda:ABSENT) RECON_C_ABSENT=$((RECON_C_ABSENT + 1)) ;;
    C-lambda:NOT_APPLICABLE) RECON_C_NOT_APPLICABLE=$((RECON_C_NOT_APPLICABLE + 1)) ;;
    C-lambda:UNMAPPED) RECON_C_UNMAPPED=$((RECON_C_UNMAPPED + 1)) ;;
    C-lambda:UNREADABLE) RECON_C_UNREADABLE=$((RECON_C_UNREADABLE + 1)) ;;
    B-s3:PATCH) RECON_B_PATCH=$((RECON_B_PATCH + 1)) ;;
    B-s3:BASE) RECON_B_BASE=$((RECON_B_BASE + 1)) ;;
    B-s3:UNKNOWN) RECON_B_UNKNOWN=$((RECON_B_UNKNOWN + 1)) ;;
    B-s3:ABSENT) RECON_B_ABSENT=$((RECON_B_ABSENT + 1)) ;;
    B-s3:NOT_APPLICABLE) RECON_B_NOT_APPLICABLE=$((RECON_B_NOT_APPLICABLE + 1)) ;;
    B-s3:UNMAPPED) RECON_B_UNMAPPED=$((RECON_B_UNMAPPED + 1)) ;;
    B-s3:UNREADABLE) RECON_B_UNREADABLE=$((RECON_B_UNREADABLE + 1)) ;;
  esac
}

reconcile_record() {
  local layer="$1" source="$2" artifact="$3" change="$4" place="$5" state="$6"
  local digest="${7:--}"
  printf 'reconcile row layer=%s source=%s artifact=%s change=%s place=%s state=%s sha256=%s\n' \
    "$layer" "$source" "$artifact" "$change" "$place" "$state" "$digest"
  reconcile_count_state "$layer" "$state"
  if [ "$layer" = "B-s3" ] && [ -n "${RECON_STATE_FILE:-}" ]; then
    printf '%s\t%s\t%s\t%s\n' "$source" "$artifact" "$place" "$state" \
      >> "$RECON_STATE_FILE"
  fi
}

reconcile_classify_hash() {
  local actual="$1" base="$2" patch="$3"
  if [ -n "$patch" ] && [ "$actual" = "$patch" ]; then
    echo PATCH
  elif [ -n "$base" ] && [ "$actual" = "$base" ]; then
    echo BASE
  else
    echo UNKNOWN
  fi
}

reconcile_lambda_mark_package() {
  local function_name="$1" prefix="$2" state="$3"
  local source artifact change base patch rel
  while IFS=$'\x1f' read -r source artifact change base patch; do
    [ -n "$source" ] || continue
    rel="${artifact#"$prefix"}"
    reconcile_record C-lambda "$source" "$artifact" "$change" \
      "lambda:${function_name}:${rel}" "$state" -
  done < <(
    jq -r --arg prefix "$prefix" '
      .paths | to_entries[]
      | select(.value.layer == "C-lambda" and .value.artifact != null)
      | select(.value.artifact | startswith($prefix))
      | [
          .key, .value.artifact, .value.change,
          (.value.base_sha256 // ""), (.value.patch_sha256 // "")
        ]
      | join("\u001f")
    ' "${KITDIR}/manifest.json"
  )
}

reconcile_lambda_function() {
  local function_name="$1" prefix="$2" work location package_entries
  local source artifact change base patch rel entry member digest state
  work="$(mktemp -d)"
  if ! location="$(aws_ lambda get-function --function-name "$function_name" \
      --query 'Code.Location' --output text 2>"${work}/error")"; then
    if grep -Eq 'ResourceNotFoundException|Function not found' "${work}/error"; then
      # A function that is not deployed in this environment is NOT_APPLICABLE, matching
      # what apply_control_overlay already does with it ("no deployed target for its
      # module" — it skips and continues). Calling it ABSENT made reconcile report drift
      # for a module that has nowhere to be delivered, which failed an otherwise fully
      # converged run.
      reconcile_lambda_mark_package "$function_name" "$prefix" NOT_APPLICABLE
    else
      reconcile_lambda_mark_package "$function_name" "$prefix" UNREADABLE
    fi
    rm -f "${work}/error"
    rmdir "$work" 2>/dev/null || true
    return
  fi
  if [ -z "$location" ] || [ "$location" = "None" ] \
      || ! curl -fsS -o "${work}/package.zip" "$location" \
      || ! unzip -Z1 "${work}/package.zip" > "${work}/entries" 2>/dev/null; then
    reconcile_lambda_mark_package "$function_name" "$prefix" UNREADABLE
    rm -f "${work}/error" "${work}/package.zip" "${work}/entries"
    rmdir "$work" 2>/dev/null || true
    return
  fi
  package_entries="${work}/entries"
  while IFS=$'\x1f' read -r source artifact change base patch; do
    [ -n "$source" ] || continue
    rel="${artifact#"$prefix"}"
    entry="$rel"
    if ! grep -Fx -- "$entry" "$package_entries" >/dev/null; then
      entry="./$rel"
    fi
    if ! grep -Fx -- "$entry" "$package_entries" >/dev/null; then
      reconcile_record C-lambda "$source" "$artifact" "$change" \
        "lambda:${function_name}:${rel}" ABSENT -
      continue
    fi
    member="${work}/member"
    if ! unzip -p "${work}/package.zip" "$entry" > "$member" 2>/dev/null; then
      reconcile_record C-lambda "$source" "$artifact" "$change" \
        "lambda:${function_name}:${rel}" UNREADABLE -
      continue
    fi
    digest="$(sha256_file "$member")"
    state="$(reconcile_classify_hash "$digest" "$base" "$patch")"
    reconcile_record C-lambda "$source" "$artifact" "$change" \
      "lambda:${function_name}:${rel}" "$state" "$digest"
  done < <(
    jq -r --arg prefix "$prefix" '
      .paths | to_entries[]
      | select(.value.layer == "C-lambda" and .value.artifact != null)
      | select(.value.artifact | startswith($prefix))
      | [
          .key, .value.artifact, .value.change,
          (.value.base_sha256 // ""), (.value.patch_sha256 // "")
        ]
      | join("\u001f")
    ' "${KITDIR}/manifest.json"
  )
  rm -f "${work}/error" "${work}/package.zip" "${work}/entries" "${work}/member"
  rmdir "$work" 2>/dev/null || true
}

reconcile_b_place_map() {
  local kit_artifact="$1"
  RECON_B_S3_KEY=""
  RECON_B_HOST_PATH=""
  # A name-derived guess produced false ABSENT rows on a real fleet. Keep this
  # measured place map explicit:
  # kit artifact                  S3 key under <assets bucket>                                      host path
  # launch-vm.sh.patched          deployment/scripts/launch-vm.sh                                  /home/ubuntu/launch-vm.sh
  # stop-vm.sh.patched            deployment/scripts/stop-vm.sh                                    /home/ubuntu/stop-vm.sh
  # backup-data.sh.patched        deployment/scripts/backup-data.sh                                /home/ubuntu/backup-data.sh
  # reset-vm.sh.patched           deployment/scripts/reset-vm.sh                                  /home/ubuntu/reset-vm.sh
  # rebuild-vm.sh.patched         deployment/scripts/rebuild-vm.sh                                /home/ubuntu/rebuild-vm.sh
  # delete-vm.sh.patched          deployment/scripts/delete-vm.sh                                  /home/ubuntu/delete-vm.sh
  # host-agent.py.patched         deployment/scripts/host-agent.py                                 /opt/openclaw/host-agent.py
  # host-agent.service.patched    NOT_PUBLISHED                                                     /etc/systemd/system/host-agent.service
  # init-host.sh.patched          deployment/bootstrap/host/<patched asset prefix>/init-host.sh    NOT_INSTALLED_ON_DISK
  # provision-host.sh.patched     NOT_PUBLISHED                                                     /opt/openclaw/provision-host.sh
  case "$kit_artifact" in
    launch-vm.sh.patched)
      RECON_B_S3_KEY="deployment/scripts/launch-vm.sh"
      RECON_B_HOST_PATH="/home/ubuntu/launch-vm.sh"
      ;;
    stop-vm.sh.patched)
      RECON_B_S3_KEY="deployment/scripts/stop-vm.sh"
      RECON_B_HOST_PATH="/home/ubuntu/stop-vm.sh"
      ;;
    backup-data.sh.patched)
      RECON_B_S3_KEY="deployment/scripts/backup-data.sh"
      RECON_B_HOST_PATH="/home/ubuntu/backup-data.sh"
      ;;
    reset-vm.sh.patched)
      RECON_B_S3_KEY="deployment/scripts/reset-vm.sh"
      RECON_B_HOST_PATH="/home/ubuntu/reset-vm.sh"
      ;;
    rebuild-vm.sh.patched)
      RECON_B_S3_KEY="deployment/scripts/rebuild-vm.sh"
      RECON_B_HOST_PATH="/home/ubuntu/rebuild-vm.sh"
      ;;
    delete-vm.sh.patched)
      RECON_B_S3_KEY="deployment/scripts/delete-vm.sh"
      RECON_B_HOST_PATH="/home/ubuntu/delete-vm.sh"
      ;;
    host-agent.py.patched)
      RECON_B_S3_KEY="deployment/scripts/host-agent.py"
      RECON_B_HOST_PATH="/opt/openclaw/host-agent.py"
      ;;
    host-agent.service.patched)
      RECON_B_S3_KEY="NOT_PUBLISHED"
      RECON_B_HOST_PATH="/etc/systemd/system/host-agent.service"
      ;;
    init-host.sh.patched)
      RECON_B_S3_KEY="${RECON_B_BOOTSTRAP_KEY:-UNREADABLE}"
      RECON_B_HOST_PATH="NOT_INSTALLED_ON_DISK"
      ;;
    provision-host.sh.patched)
      RECON_B_S3_KEY="NOT_PUBLISHED"
      RECON_B_HOST_PATH="/opt/openclaw/provision-host.sh"
      ;;
    *)
      return 1
      ;;
  esac
}

reconcile_b_mark_instance() {
  local records_file="$1" instance_id="$2" state="$3"
  local source artifact change base patch key host_path
  while IFS=$'\x1f' read -r source artifact change base patch key host_path; do
    [ -n "$source" ] || continue
    reconcile_record B-s3 "$source" "$artifact" "$change" \
      "instance:${instance_id}:${host_path}" "$state" -
  done < "$records_file"
}

reconcile_b_instance() {
  local records_file="$1" instance_id="$2" ssm_params="$3"
  local command_id result status output source artifact change base patch key host_path value state detail
  command_id="$(aws_ ssm send-command --instance-ids "$instance_id" \
    --document-name AWS-RunShellScript --comment "restorepatch manifest reconcile" \
    --parameters "$ssm_params" --timeout-seconds 120 \
    --query 'Command.CommandId' --output text 2>/dev/null)" || command_id=""
  if [ -z "$command_id" ] || [ "$command_id" = "None" ]; then
    reconcile_b_mark_instance "$records_file" "$instance_id" UNREADABLE
    return
  fi
  aws_ ssm wait command-executed --command-id "$command_id" \
    --instance-id "$instance_id" >/dev/null 2>&1 || true
  result="$(aws_ ssm get-command-invocation --command-id "$command_id" \
    --instance-id "$instance_id" --output json 2>/dev/null)" || result=""
  if [ -z "$result" ]; then
    reconcile_b_mark_instance "$records_file" "$instance_id" UNREADABLE
    return
  fi
  status="$(printf '%s' "$result" | jq -r '.Status // "Unknown"')"
  if [ "$status" != "Success" ]; then
    reconcile_b_mark_instance "$records_file" "$instance_id" UNREADABLE
    return
  fi
  output="$(printf '%s' "$result" | jq -r '.StandardOutputContent // ""')"
  while IFS=$'\x1f' read -r source artifact change base patch key host_path; do
    [ -n "$source" ] || continue
    if ! value="$(printf '%s\n' "$output" | awk -F '\t' -v wanted="$artifact" '
        $1 == wanted {gsub(/\r/, "", $2); print $2; found=1; exit}
        END {if (!found) exit 1}
      ')"; then
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "instance:${instance_id}:${host_path}" UNREADABLE -
      continue
    fi
    case "$value" in
      ABSENT|UNREADABLE) state="$value" ;;
      *)
        if printf '%s' "$value" | grep -Eq '^[0-9a-f]{64}$'; then
          state="$(reconcile_classify_hash "$value" "$base" "$patch")"
        else
          state=UNREADABLE
          value=-
        fi
        ;;
    esac
    # A `case` cannot live inside `$( )`: the first pattern's closing paren ends the
    # command substitution, so the rest of the branch leaks out as a bare `;;` — which
    # `bash -n` accepts and only fails at run time, in the middle of a fleet sweep.
    case "$state" in
      PATCH|BASE|UNKNOWN) detail="$value" ;;
      *) detail=- ;;
    esac
    reconcile_record B-s3 "$source" "$artifact" "$change" \
      "instance:${instance_id}:${host_path}" "$state" "$detail"
  done < "$records_file"
}

reconcile_b_s3() {
  local manifest_records_file records_file host_records_file work
  local source artifact change base patch name key host_path digest state detail
  local asg_json instance_ids ssm_params instance_id readable_states state_count details
  local bootstrap_userdata bootstrap_prefix
  work="$(mktemp -d)"
  manifest_records_file="${work}/manifest-records.usv"
  records_file="${work}/records.usv"
  host_records_file="${work}/host-records.usv"
  RECON_B_BOOTSTRAP_KEY=""
  if bootstrap_userdata="$(aws_ ec2 describe-launch-template-versions \
      --launch-template-id "$LT_ID" --versions "$ASG_LT_REF" \
      --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
      --output text 2>/dev/null | base64 -d 2>/dev/null)"; then
    bootstrap_prefix="$(printf '%s' "$bootstrap_userdata" \
      | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
    [ -z "$bootstrap_prefix" ] \
      || RECON_B_BOOTSTRAP_KEY="deployment/bootstrap/host/${bootstrap_prefix}/init-host.sh"
  fi
  jq -r '
    .paths | to_entries[]
    | select(.value.layer == "B-s3" and .value.artifact != null)
    | [
        .key, .value.artifact, .value.change,
        (.value.base_sha256 // ""), (.value.patch_sha256 // "")
      ]
    | join("\u001f")
  ' "${KITDIR}/manifest.json" > "$manifest_records_file" \
    || die "cannot read B-s3 records from manifest"

  : > "$records_file"
  : > "$host_records_file"
  while IFS=$'\x1f' read -r source artifact change base patch; do
    [ -n "$source" ] || continue
    name="${artifact#host-scripts/}"
    if ! reconcile_b_place_map "$name"; then
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "place-map:${name}" UNMAPPED -
      continue
    fi
    key="$RECON_B_S3_KEY"
    host_path="$RECON_B_HOST_PATH"
    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' \
      "$source" "$artifact" "$change" "$base" "$patch" "$key" "$host_path" \
      >> "$records_file"
    if [ "$key" = "NOT_PUBLISHED" ]; then
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "s3:not-published" NOT_APPLICABLE -
    fi
    if [ "$host_path" = "NOT_INSTALLED_ON_DISK" ]; then
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "host:not-installed-on-disk" NOT_APPLICABLE -
    else
      printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' \
        "$source" "$artifact" "$change" "$base" "$patch" "$key" "$host_path" \
        >> "$host_records_file"
    fi
  done < "$manifest_records_file"

  while IFS=$'\x1f' read -r source artifact change base patch key host_path; do
    [ -n "$source" ] || continue
    [ "$key" != "NOT_PUBLISHED" ] || continue
    if [ "$key" = "UNREADABLE" ]; then
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "launch-template:${LT_ID}:${ASG_LT_REF}" UNREADABLE -
      continue
    fi
    if aws_ s3api head-object --bucket "$BUCKET" --key "$key" \
        >/dev/null 2>"${work}/s3-error"; then
      if aws_ s3 cp "s3://${BUCKET}/${key}" "${work}/s3-object" \
          --no-progress >/dev/null 2>"${work}/s3-error"; then
        digest="$(sha256_file "${work}/s3-object")"
        if [ "$artifact" = "host-scripts/init-host.sh.patched" ]; then
          if ( render_bootstrap_artifact "${KITDIR}/host-scripts/init-host.sh.patched" \
               "${work}/s3-object" "${work}/rendered-init-host.sh" ); then
            if python3 - "${work}/s3-object" "${work}/rendered-init-host.sh" <<'PY'
import pathlib
import re
import sys

# Same judgement as verify's bootstrap-prefix fallback below; change both together.
def code_only(p):
    # NOTE: this same judgement is implemented at four sites in this file
    # (resolve_bootstrap_state, reconcile_b_s3, and twice in verify). Change them
    # together — a divergent copy makes two gates disagree about one environment.
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
              state=PATCH
            else
              state=UNKNOWN
            fi
          else
            state=UNREADABLE
          fi
        else
          state="$(reconcile_classify_hash "$digest" "$base" "$patch")"
        fi
        reconcile_record B-s3 "$source" "$artifact" "$change" \
          "s3:${BUCKET}/${key}" "$state" "$digest"
      else
        reconcile_record B-s3 "$source" "$artifact" "$change" \
          "s3:${BUCKET}/${key}" UNREADABLE -
      fi
    elif grep -Eq '(\(404\)|NoSuchKey|NotFound|Not Found)' "${work}/s3-error"; then
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "s3:${BUCKET}/${key}" ABSENT -
    else
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "s3:${BUCKET}/${key}" UNREADABLE -
    fi
  done < "$records_file"

  if ! asg_json="$(aws_ autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$ASG" --output json 2>"${work}/asg-error")" \
      || ! printf '%s' "$asg_json" |
        jq -e '.AutoScalingGroups | length == 1' >/dev/null; then
    while IFS=$'\x1f' read -r source artifact change base patch key host_path; do
      [ -n "$source" ] || continue
      reconcile_record B-s3 "$source" "$artifact" "$change" \
        "asg:${ASG}" UNREADABLE -
    done < "$host_records_file"
  else
    instance_ids="$(printf '%s' "$asg_json" |
      jq -r '.AutoScalingGroups[0].Instances[]?.InstanceId')"
    # The generator runs as a plain command with its output redirected, NOT inside
    # `$( )`. A heredoc whose body carries shell-significant characters inside a
    # command substitution does not parse here: bash reports `unexpected EOF while
    # looking for matching ')'` at the substitution line, and `bash -n` blames a line
    # ~100 lines later, which makes it look like an unrelated defect.
    python3 - "$host_records_file" > "${work}/ssm-params.json" 2>/dev/null <<'PY'
import json
import shlex
import sys

lines = ["set +e"]
with open(sys.argv[1], encoding="utf-8") as handle:
    for raw in handle:
        fields = raw.rstrip("\n").split("\x1f")
        if len(fields) < 7:
            continue
        artifact = fields[1]
        path = fields[6]
        lines.extend([
            f"artifact={shlex.quote(artifact)}",
            f"path={shlex.quote(path)}",
            'if [ ! -e "$path" ]; then',
            '  printf "%s\\tABSENT\\n" "$artifact"',
            'elif [ ! -f "$path" ]; then',
            '  printf "%s\\tUNREADABLE\\n" "$artifact"',
            "else",
            '  digest="$(sha256sum -- "$path" 2>/dev/null | awk \'{print $1}\')"',
            '  if printf "%s" "$digest" | grep -Eq "^[0-9a-f]{64}$"; then',
            '    printf "%s\\t%s\\n" "$artifact" "$digest"',
            "  else",
            '    printf "%s\\tUNREADABLE\\n" "$artifact"',
            "  fi",
            "fi",
        ])
print(json.dumps({"commands": ["\n".join(lines)]}, separators=(",", ":")))
PY
    # An empty file (generator failed, or produced nothing) keeps the existing
    # "no params" branch below, which marks every instance UNREADABLE rather than
    # reporting a converged sweep it never performed.
    ssm_params="$(cat "${work}/ssm-params.json" 2>/dev/null || true)"
    if [ -z "$ssm_params" ]; then
      for instance_id in $instance_ids; do
        reconcile_b_mark_instance "$host_records_file" "$instance_id" UNREADABLE
      done
    else
      for instance_id in $instance_ids; do
        reconcile_b_instance "$host_records_file" "$instance_id" "$ssm_params"
      done
    fi
  fi

  while IFS=$'\x1f' read -r source artifact change base patch key host_path; do
    [ -n "$source" ] || continue
    [ "$key" != "NOT_PUBLISHED" ] || continue
    [ "$host_path" != "NOT_INSTALLED_ON_DISK" ] || continue
    readable_states="$(awk -F '\t' -v wanted="$artifact" '
      $2 == wanted && $4 != "UNREADABLE" && $4 != "NOT_APPLICABLE" &&
        $4 != "UNMAPPED" {print $4}
    ' "$RECON_STATE_FILE" | sort -u)"
    state_count="$(printf '%s\n' "$readable_states" |
      awk 'NF {count++} END {print count+0}')"
    if [ "$state_count" -gt 1 ]; then
      details="$(awk -F '\t' -v wanted="$artifact" '
        $2 == wanted {printf "%s%s=%s", (seen++ ? "," : ""), $3, $4}
      ' "$RECON_STATE_FILE")"
      echo "reconcile SPLIT source=$source artifact=$artifact states=$details"
      RECON_SPLIT=$((RECON_SPLIT + 1))
    fi
  done < "$records_file"

  rm -f "$manifest_records_file" "$records_file" "$host_records_file" \
    "${work}/s3-error" "${work}/s3-object" "${work}/asg-error" \
    "${work}/ssm-params.json" "${work}/rendered-init-host.sh"
  rmdir "$work" 2>/dev/null || true
}

reconcile_run() {
  local manifest="${KITDIR}/manifest.json" peer_fn nonpatch verdict
  [ -f "$manifest" ] || die "manifest not found: $manifest"
  jq -e '.paths | type == "object"' "$manifest" >/dev/null \
    || die "manifest paths must be an object"
  reconcile_reset_counts
  RECON_STATE_FILE="$(mktemp)"
  say "reconcile scope=$VERIFY_SCOPE (READ-ONLY)"

  if scope_includes control; then
    case "$PEER_DISCOVERY_CONFIRMED" in
      True|true) ;;
      *)
        # If discovery could not enumerate the complete API-package serving set,
        # checking only the known function would make unknown coverage look converged.
        reconcile_record C-lambda discovery "lambda/api/*" - \
          "lambda:peer-discovery" UNREADABLE -
        ;;
    esac
    reconcile_lambda_function "$FN" "lambda/api/"
    for peer_fn in $PEER_FNS; do
      reconcile_lambda_function "$peer_fn" "lambda/api/"
    done
    reconcile_lambda_function openclaw-backup "lambda/backup/"
    reconcile_lambda_function openclaw-health-check "lambda/health_check/"
    reconcile_lambda_function openclaw-scaler "lambda/scaler/"
    reconcile_lambda_function openclaw-tenant-stats-writer "lambda/tenant_stats/"
    echo "reconcile summary layer=C-lambda patch=$RECON_C_PATCH base=$RECON_C_BASE unknown=$RECON_C_UNKNOWN absent=$RECON_C_ABSENT not_applicable=$RECON_C_NOT_APPLICABLE unmapped=$RECON_C_UNMAPPED unreadable=$RECON_C_UNREADABLE"
  fi
  if scope_includes data; then
    reconcile_b_s3
    echo "reconcile summary layer=B-s3 patch=$RECON_B_PATCH base=$RECON_B_BASE unknown=$RECON_B_UNKNOWN absent=$RECON_B_ABSENT not_applicable=$RECON_B_NOT_APPLICABLE unmapped=$RECON_B_UNMAPPED unreadable=$RECON_B_UNREADABLE"
  fi

  nonpatch=$((RECON_BASE + RECON_UNKNOWN + RECON_ABSENT + RECON_UNMAPPED + RECON_UNREADABLE))
  if [ "$nonpatch" -eq 0 ] && [ "$RECON_SPLIT" -eq 0 ] \
      && [ "$RECON_PATCH" -gt 0 ]; then
    verdict=CONVERGED
  else
    verdict=DRIFTED
  fi
  echo "reconcile verdict=$verdict patch=$RECON_PATCH base=$RECON_BASE unknown=$RECON_UNKNOWN absent=$RECON_ABSENT not_applicable=$RECON_NOT_APPLICABLE unmapped=$RECON_UNMAPPED split=$RECON_SPLIT unreadable=$RECON_UNREADABLE"
  rm -f "$RECON_STATE_FILE"
  RECON_STATE_FILE=""
  [ "$verdict" = "CONVERGED" ]
}

case "$PHASE" in

reconcile)
  reconcile_run
  exit $?
  ;;

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
  overlay_imports_resolve_against "$FN" "$work"
  # One concern failing must not deny the others a verdict, so record it instead of dying.
  # apply re-checks this independently and refuses the overlay step on its own.
  if [ $? -ne 0 ]; then
    IMPORT_STATE=BLOCKED
  else
    IMPORT_STATE=OK
  fi
  # The overlay is applied to the peers too (see the apply-api overlay loop), so the same
  # question has to be asked of each peer's own live package. A peer can legitimately carry
  # a different module set than the primary function, which is why counting modules is not
  # enough -- the imports have to resolve against that peer's package.
  for peer_fn in $PEER_FNS; do
    say "overlay import resolution against the LIVE package of peer $peer_fn"
    overlay_imports_resolve_against "$peer_fn" || IMPORT_STATE=BLOCKED
  done
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
  echo "   function=$FN ready=$ov_ready already=$ov_already"
  overlay_scope_blocked=0
  case "$PEER_DISCOVERY_CONFIRMED" in
    True|true) ;;
    *)
      echo "   peer discovery: BLOCKED (rerun discover-env.sh from a host that can enumerate Lambda)"
      overlay_scope_blocked=1
      ;;
  esac
  if [ -n "$FN_ESM_QUALIFIER" ]; then
    echo "   function=$FN BLOCKED pinned ESM qualifier=$FN_ESM_QUALIFIER"
    overlay_scope_blocked=1
  fi
  for peer_fn in $PEER_FNS; do
    peer_ready=0; peer_already=0
    peer_work="$(mktemp -d)"
    peer_loc="$(aws_ lambda get-function --function-name "$peer_fn" \
      --query 'Code.Location' --output text)" \
      || die "cannot locate peer package for $peer_fn"
    curl -fsS -o "${peer_work}/live.zip" "$peer_loc" \
      || die "cannot download peer package for $peer_fn"
    (cd "$peer_work" && unzip -oq live.zip) \
      || die "cannot unpack peer package for $peer_fn"
    while IFS= read -r rel; do
      case "$rel" in
        __pycache__/*|*/__pycache__/*|*.pyc|*.pyo|.DS_Store|*/.DS_Store)
          continue
          ;;
      esac
      kf="${KITDIR}/lambda/api/${rel}"
      lf="${peer_work}/${rel}"
      if [ ! -f "$lf" ]; then
        peer_ready=$((peer_ready + 1))
        continue
      fi
      a="$(sha256_file "$kf")"
      b="$(sha256_file "$lf")"
      if [ "$a" = "$b" ]; then
        peer_already=$((peer_already + 1))
      else
        peer_ready=$((peer_ready + 1))
      fi
    done < <(cd "${KITDIR}/lambda/api" && find . -type f -print | sed 's|^\./||' | sort)
    echo "   function=$peer_fn ready=$peer_ready already=$peer_already"
    ov_ready=$((ov_ready + peer_ready))
    ov_already=$((ov_already + peer_already))
    rm -rf "$peer_work"
  done
  while IFS=$'\t' read -r peer_fn peer_qualifier; do
    [ -n "$peer_fn" ] || continue
    if [ -n "$peer_qualifier" ]; then
      echo "   function=$peer_fn BLOCKED pinned ESM qualifier=$peer_qualifier"
      overlay_scope_blocked=1
    fi
  done <<< "$PEER_ESM_QUALIFIERS"
  if [ "$overlay_scope_blocked" -ne 0 ]; then
    echo "   STATE overlay=BLOCKED the complete serving scope is not safe to overlay"
    OVERLAY_STATE=BLOCKED
  elif [ "$ov_ready" -eq 0 ]; then
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
  if [ "${OVERLAY_STATE:-BLOCKED}" = "BLOCKED" ]; then
    echo "   RESULT BLOCKED — the API-package serving scope is not safe to overlay."
    exit 1
  fi
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
  validate_control_overlay_scope
  say "recording restore points into $STATE"
  mkdir -p "$BACKUP_DIR" || die "cannot create backup directory $BACKUP_DIR"
  hook_to="$(aws_ autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" \
    --query "LifecycleHooks[?LifecycleHookName=='${HOOK_NAME}'].HeartbeatTimeout|[0]" --output text)"
  backup_anchor_put hook_backup_timeout "$hook_to"
  hook_dr="$(aws_ autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" \
    --query "LifecycleHooks[?LifecycleHookName=='${HOOK_NAME}'].DefaultResult|[0]" --output text)"
  [ -n "$hook_dr" ] && [ "$hook_dr" != "None" ] \
    || die "cannot read the current DefaultResult of hook $HOOK_NAME; refusing to overwrite a setting we could not record"
  backup_anchor_put hook_backup_default_result "$hook_dr"
  # Verify and rollback need the exact pre-apply capacity values, including a legitimate zero minimum.
  read -r asg_min asg_desired <<< "$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].[MinSize,DesiredCapacity]' --output text)"
  backup_anchor_put asg_backup_min_size "$asg_min"
  backup_anchor_put asg_backup_desired "$asg_desired"
  lt_def="$(aws_ ec2 describe-launch-templates --launch-template-id "$LT_ID" \
    --query 'LaunchTemplates[0].DefaultVersionNumber' --output text)"
  backup_anchor_put host_lt_backup_version "$lt_def"
  lt_ami="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions "$LT_VER" --query 'LaunchTemplateVersions[0].LaunchTemplateData.ImageId' --output text)"
  backup_anchor_put host_lt_backup_ami "$lt_ami"
  env_n="$(aws_ lambda get-function-configuration --function-name "$FN" \
    --query 'length(Environment.Variables)' --output text)"
  backup_anchor_put api_env_key_count "$env_n"
  ver="$(state_get api_backup_version)"
  if [ -n "$ver" ] && [ "$REANCHOR" -eq 0 ]; then
    backup_anchor_put api_backup_version "$ver"
  else
    ver="$(aws_ lambda publish-version --function-name "$FN" \
      --description pre-restorepatch-anchor --query Version --output text)" \
      || die "cannot publish anchor version"
    backup_anchor_put api_backup_version "$ver"
  fi
  alias_anchor="$(state_get api_alias_name)"
  alias_version_anchor="$(state_get api_alias_backup_version)"
  if [ "$REANCHOR" -eq 0 ] && { [ -n "$alias_anchor" ] || [ -n "$alias_version_anchor" ]; }; then
    [ -n "$alias_anchor" ] && [ -n "$alias_version_anchor" ] \
      || die "API alias backup anchor is incomplete; use --reanchor to replace it"
    backup_anchor_put api_alias_name "$alias_anchor"
    backup_anchor_put api_alias_backup_version "$alias_version_anchor"
  elif an="$(alias_name)"; then
    backup_anchor_put api_alias_name "$an"
    backup_anchor_put api_alias_backup_version "$(aws_ lambda get-alias --function-name "$FN" \
      --name "$an" --query FunctionVersion --output text)"
  else
    echo "   API environment does not use an alias; the unqualified version is the serving path"
  fi
  backup_function "$FN" api_backup_zip_sha256
  for peer_fn in $PEER_FNS; do
    backup_function "$peer_fn"
  done
  for extra_fn in openclaw-backup openclaw-health-check openclaw-scaler \
    openclaw-tenant-stats-writer openclaw-console-bff; do
    if [ "$REANCHOR" -eq 0 ] && [ -n "$(state_get "backup_${extra_fn}")" ]; then
      backup_function "$extra_fn"
    elif aws_ lambda get-function --function-name "$extra_fn" >/dev/null 2>&1; then
      backup_function "$extra_fn"
    else
      echo "   ABSENT $extra_fn; no recovery package required"
    fi
  done
  hook_to="$(state_get hook_backup_timeout)"
  asg_min="$(state_get asg_backup_min_size)"
  asg_desired="$(state_get asg_backup_desired)"
  lt_def="$(state_get host_lt_backup_version)"
  lt_ami="$(state_get host_lt_backup_ami)"
  env_n="$(state_get api_env_key_count)"
  ver="$(state_get api_backup_version)"
  echo "   hook=$hook_to lt_default=$lt_def ami=$lt_ami api_anchor=$ver env_keys=$env_n"
  echo "   asg_min=$asg_min asg_desired=$asg_desired"
  say "backup OK"
  ;;

apply)
  [ -f "$STATE" ] || die "no backup state; run backup first"
  [ -n "$AMI" ] || die "new_ami_id missing from environment; bake the AMI per host-scripts/packer/CUSTOMER-GUIDE.md first, or use apply-control for the control plane only"
  # Refuse before any concern writes when the complete API-package serving scope
  # is unknown or a mapping would stay pinned to an old published version.
  validate_control_overlay_scope

  # This call used to pass `--default-result ABANDON` as a literal, so widening the heartbeat
  # also overwrote whatever the environment had chosen for the expiry behaviour -- and the
  # rollback path restored that same literal rather than the original value. (Omitting the
  # flag is not the issue: a probe on a throwaway ASG confirmed the API preserves an existing
  # DefaultResult when the field is absent, CONTINUE stayed CONTINUE and ABANDON stayed
  # ABANDON. The problem was writing a constant, not leaving the field out.) Passing the
  # recorded value keeps this phase from deciding the expiry behaviour on the operator's
  # behalf, and lets rollback put back what was actually there.
  hook_dr="$(state_get hook_backup_default_result)"
  [ -n "$hook_dr" ] || die "backup state has no hook_backup_default_result; re-run backup"
  say "1/3 widen $HOOK_NAME heartbeat to ${NEW_TIMEOUT}s (DefaultResult stays $hook_dr)"
  aws_ autoscaling put-lifecycle-hook --auto-scaling-group-name "$ASG" \
    --lifecycle-hook-name "$HOOK_NAME" \
    --lifecycle-transition autoscaling:EC2_INSTANCE_LAUNCHING \
    --heartbeat-timeout "$NEW_TIMEOUT" --default-result "$hook_dr" || die "hook update failed"

  assert_asg_lt_promotable
  say "2/3 upload bootstrap script to its content-addressed key, then one new LT version"
  art="${KITDIR}/host-scripts/init-host.sh.patched"
  [ -f "$art" ] || die "missing artifact $art"
  expected_artifact_sha="$(
    python3 - "${KITDIR}/manifest.json" 2>/dev/null <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(manifest["paths"]["deploy/userdata/init-host.sh"]["patch_sha256"])
PY
  )" || die "cannot read init-host artifact digest from manifest"
  printf '%s' "$expected_artifact_sha" | grep -Eq '^[0-9a-f]{64}$' \
    || die "manifest has no valid patch_sha256 for deploy/userdata/init-host.sh"
  got="$(sha256_file "$art")"
  [ "$got" = "$expected_artifact_sha" ] \
    || die "artifact sha256 $got does not match expected file digest $expected_artifact_sha"
  # Recompute the state here rather than trusting precheck: the run directory is editable
  # and the live target may have moved between the two phases.
  lt_default_data="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
    --versions "$ASG_LT_REF" \
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
  publish_host_assets "$live_art"
  render_bootstrap_artifact "$art" "$live_art" "$rendered_art"
  rendered_sha="$(sha256_file "$rendered_art")"
  state_put rendered_bootstrap_sha256 "$rendered_sha"
  state_put rendered_bootstrap_source_prefix "$live_prefix"
  # The bootstrap key is CONTENT-ADDRESSED, and the boot path proves it: the rendered
  # UserData verifies the downloaded object with `sha256sum -c` against the same 64-hex it
  # took from the key. ASSET_BUNDLE_PREFIX is the hash of the AUTHOR's rendering, so any
  # environment that renders different values (i.e. anyone supplying --values, or any
  # deployment with different subnets/ratios) would publish content whose sha256 does NOT
  # equal its key, and every new host would fail verification and ABANDON. Use the hash of
  # what we actually rendered; it is already computed on the line above.
  publish_s3_asset "$rendered_art" \
    "deployment/bootstrap/host/${rendered_sha}/init-host.sh"
  rm -f "$live_art"

  printf '%s' "$live_userdata" \
    | base64 -d > /tmp/restorepatch-ud.txt || die "cannot read in-service UserData"
  # Replace every observed content-addressed bootstrap prefix. The assertions below
  # forbid a no-op rewrite from creating and promoting an ineffective LT version.
  python3 - /tmp/restorepatch-ud.txt "$rendered_sha" \
    "$rendered_art" <<'PY'
import re
import sys
p, new, rendered_path = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(p, encoding="utf-8", errors="surrogateescape").read()
# The prefix appears in the UserData in more than one ROLE: as the S3 key path, as the
# expected digest fed to `sha256sum -c`, and inside the confirmation echo. Rewriting only
# the path-shaped occurrences leaves the downloader fetching the NEW object while verifying
# the OLD digest, so `set -euo pipefail` aborts the bootstrap and the lifecycle hook
# ABANDONs every new host. Because the scheme is content-addressed, path == digest, so
# substituting every 64-hex occurrence is correct and keeps the roles consistent.
bare = re.compile(r"\b[0-9a-f]{64}\b")
old = sorted({m.group(0) for m in bare.finditer(t) if m.group(0) != new})
if not old:
    raise SystemExit("FAIL: no old bootstrap prefix was found for substitution")
t = bare.sub(lambda m: new if m.group(0) != new else m.group(0), t)
if "deployment/bootstrap/host/" + new not in t:
    raise SystemExit("FAIL: target bootstrap prefix absent after substitution")
# Whole-text check, not path-scoped: a stale digest outside a path is exactly the failure
# the path-scoped check used to wave through.
remaining = sorted({m.group(0) for m in bare.finditer(t) if m.group(0) != new})
if remaining:
    raise SystemExit("FAIL: old bootstrap digests remain after substitution: " + ",".join(remaining))
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
  # GNU coreutils `base64 -i` is --ignore-garbage (not BSD's "input file"), so BSD-first
  # probing SUCCEEDS on GNU while still wrapping at 76 columns. Those newlines land inside
  # the --launch-template-data JSON string and abort the call with
  # "Invalid control character at: line 1 column ...". Use the portable form already used
  # by lib/apply-lt.sh.
  b64="$(base64 < /tmp/restorepatch-ud.txt | tr -d '\n')"
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
  # Both gates above stay TRUE forever after one success -- neither key is ever cleared, and the
  # state file lives in the kit directory, so it survives across sessions. Nothing else consulted
  # the ASG's actual refresh history, and the call below passes SkipMatching:false, which is AWS's
  # explicit "replace instances even when they already match". So a second `refresh` on a converged
  # fleet replaced every tenant-carrying host again for zero configuration change. Decide from the
  # fleet's real state, not from a record that cannot expire.
  target_refresh_lt="$(state_get host_lt_new_version)"
  [ -n "$target_refresh_lt" ] || die "refresh refused: no target launch-template version in $STATE"
  # Refuse while ANY refresh on this group is unfinished, whoever started it. Keying this off our own
  # recorded id would miss one started from the console or by another operator, leaving
  # start-instance-refresh as the only thing between us and two concurrent replacements of a
  # tenant-carrying fleet. A failed read is fatal: an unreadable history cannot prove quiescence.
  if ! refresh_history="$(aws_ autoscaling describe-instance-refreshes \
    --auto-scaling-group-name "$ASG" \
    --query 'InstanceRefreshes[].[InstanceRefreshId,Status]' --output text)"; then
    die "refresh refused: cannot read the instance-refresh history of $ASG; resolve that before starting one"
  fi
  # Terminal is an ALLOWLIST. Everything else counts as unfinished -- including Baking, which is the
  # post-update bake wait and therefore not done, and any status AWS adds later. Documented values:
  # Pending, InProgress, Successful, Failed, Cancelling, Cancelled, RollbackInProgress,
  # RollbackFailed, RollbackSuccessful, Baking.
  unfinished_refreshes=""
  while IFS="$(printf '\t')" read -r history_id history_status; do
    [ -n "$history_id" ] || continue
    case "$history_status" in
      Successful|Failed|Cancelled|RollbackFailed|RollbackSuccessful) ;;
      *) unfinished_refreshes="${unfinished_refreshes:+${unfinished_refreshes} }${history_id}=${history_status}" ;;
    esac
  done <<REFRESH_HISTORY
$refresh_history
REFRESH_HISTORY
  [ -z "$unfinished_refreshes" ] \
    || die "refresh refused: $ASG has an unfinished instance refresh ($unfinished_refreshes); wait for it to reach a terminal state instead of starting a second one"
  # Whether a refresh is NEEDED is answered by the fleet, never by a stored verdict. A recorded
  # Successful proves only that some refresh finished -- a concurrent launch-template repoint means it
  # may have converged the fleet onto a different version than this run targets.
  if ! fleet_pairs="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].Instances[].[LaunchTemplate.LaunchTemplateId,LaunchTemplate.Version]' \
    --output text)"; then
    die "cannot read the launch-template identity of the instances in $ASG"
  fi
  fleet_desired="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].DesiredCapacity' --output text)" \
    || die "cannot read the desired capacity of $ASG"
  # Compare the template IDENTITY as well as the version: version N of a different launch template is
  # a different thing, and version alone would read it as converged.
  fleet_size=0
  fleet_off_target=0
  while IFS="$(printf '\t')" read -r instance_lt_id instance_lt_version; do
    [ -n "$instance_lt_id" ] || continue
    fleet_size=$((fleet_size + 1))
    if [ "$instance_lt_id" != "$LT_ID" ] || [ "$instance_lt_version" != "$target_refresh_lt" ]; then
      fleet_off_target=1
    fi
  done <<FLEET_PAIRS
$fleet_pairs
FLEET_PAIRS
  if [ "$fleet_size" -eq 0 ]; then
    # Nothing to replace. That is a real no-op only when the group is meant to be empty; an empty
    # group that wants capacity is a problem to resolve, not a refresh to skip.
    [ "$fleet_desired" = "0" ] \
      || die "refresh refused: $ASG wants $fleet_desired instance(s) but reports none; resolve that before refreshing"
    say "SKIP refresh: $ASG is scaled to zero, so there is nothing to replace"
    echo "   NEXT: bash lib/apply-restorepatch.sh verify --env \"$ENVJSON\" --kit \"$KITDIR\""
    exit 0
  fi
  if [ "$fleet_off_target" -eq 0 ]; then
    say "SKIP refresh: all $fleet_size instance(s) in $ASG already run $LT_ID version $target_refresh_lt"
    echo "   NEXT: bash lib/apply-restorepatch.sh verify --env \"$ENVJSON\" --kit \"$KITDIR\""
    exit 0
  fi
  say "fleet has $fleet_size instance(s), not all on $LT_ID version $target_refresh_lt; a refresh is needed"
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
  # A refresh replaces instances using the ASG's CURRENT configuration, not the version this run
  # decided to converge on. So confirm, immediately before starting, that the group actually resolves
  # to the target launch template -- otherwise a concurrent repoint would make this run replace the
  # whole fleet onto someone else's version while reporting success for ours. Passing
  # --desired-configuration would pin it, but it also REWRITES the group's configuration on success,
  # which would silently clobber that concurrent repoint; refusing is the honest option.
  asg_lt_id_now="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].[LaunchTemplate.LaunchTemplateId,MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateId]' \
    --output text | tr '\t' '\n' | grep -v '^None$' | head -1)" \
    || die "cannot read the launch template currently configured on $ASG"
  asg_lt_ref_now="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].[LaunchTemplate.Version,MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.Version]' \
    --output text | tr '\t' '\n' | grep -v '^None$' | head -1)" \
    || die "cannot read the launch-template version currently configured on $ASG"
  [ -n "$asg_lt_id_now" ] && [ -n "$asg_lt_ref_now" ] \
    || die "refresh refused: $ASG does not report a launch template to replace onto"
  # Under a mixed-instances policy each override may carry its OWN LaunchTemplateSpecification, and
  # when such an override is selected the base template does not apply to those instances at all. So
  # a base-template match would be a false green: part of the fleet could be replaced onto a different
  # template or version while this run reports the target. The control plane already refuses this
  # exact shape (services/bootstrap_version_service.py::_asg_lt_spec, added after a review found the
  # same false green there); refuse it here rather than guess which override wins.
  mip_override_lt_count="$(aws_ autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG" \
    --query 'length(AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.Overrides[?LaunchTemplateSpecification!=null] || `[]`)' \
    --output text)" \
    || die "cannot read the mixed-instances overrides of $ASG"
  case "$mip_override_lt_count" in
    ''|None|0) ;;
    *)
      die "refresh refused: $ASG has $mip_override_lt_count mixed-instances override(s) carrying their own launch template, so matching the base template proves nothing about what those instances would be replaced onto; resolve the overrides before refreshing"
      ;;
  esac
  # The group may reference a floating alias. Resolve it to the concrete number the replacement would
  # actually use, because "$Default" can be repointed by anyone at any time.
  case "$asg_lt_ref_now" in
    '$Default')
      asg_lt_version_now="$(aws_ ec2 describe-launch-templates --launch-template-ids "$asg_lt_id_now" \
        --query 'LaunchTemplates[0].DefaultVersionNumber' --output text)" \
        || die "cannot resolve \$Default for $asg_lt_id_now"
      ;;
    '$Latest')
      asg_lt_version_now="$(aws_ ec2 describe-launch-templates --launch-template-ids "$asg_lt_id_now" \
        --query 'LaunchTemplates[0].LatestVersionNumber' --output text)" \
        || die "cannot resolve \$Latest for $asg_lt_id_now"
      ;;
    *) asg_lt_version_now="$asg_lt_ref_now" ;;
  esac
  { [ "$asg_lt_id_now" = "$LT_ID" ] && [ "$asg_lt_version_now" = "$target_refresh_lt" ]; } \
    || die "refresh refused: $ASG is configured for $asg_lt_id_now version $asg_lt_version_now (reference $asg_lt_ref_now) but this run targets $LT_ID version $target_refresh_lt; point the group at the target before refreshing"
  echo "   PASS group resolves to $LT_ID version $asg_lt_version_now (reference $asg_lt_ref_now)"
  # Re-check quiescence right before starting. This does not close the race -- only a shared lease
  # would -- but it shrinks the window between the history read above and the start below.
  if ! refresh_history_recheck="$(aws_ autoscaling describe-instance-refreshes \
    --auto-scaling-group-name "$ASG" \
    --query 'InstanceRefreshes[].[InstanceRefreshId,Status]' --output text)"; then
    die "refresh refused: cannot re-read the instance-refresh history of $ASG immediately before starting"
  fi
  unfinished_recheck=""
  while IFS="$(printf '\t')" read -r recheck_id recheck_status; do
    [ -n "$recheck_id" ] || continue
    case "$recheck_status" in
      Successful|Failed|Cancelled|RollbackFailed|RollbackSuccessful) ;;
      *) unfinished_recheck="${unfinished_recheck:+${unfinished_recheck} }${recheck_id}=${recheck_status}" ;;
    esac
  done <<REFRESH_RECHECK
$refresh_history_recheck
REFRESH_RECHECK
  [ -z "$unfinished_recheck" ] \
    || die "refresh refused: a refresh started on $ASG while this run was checking ($unfinished_recheck)"
  # AWS documents min=max=100 as launch-before-terminate, one-at-a-time replacement.
  refresh_id="$(aws_ autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
    --preferences '{"MinHealthyPercentage":100,"MaxHealthyPercentage":100,"InstanceWarmup":900,"SkipMatching":false}' \
    --query InstanceRefreshId --output text)" || die "cannot start instance refresh"
  case "$refresh_id" in
    ''|None) die "start-instance-refresh returned no refresh id for $ASG; treat the refresh state as unknown and inspect it before retrying" ;;
  esac
  state_put instance_refresh_id "$refresh_id" \
    || die "refresh $refresh_id started on $ASG but recording its id failed; the run is NOT idempotent from here -- inspect the group's refresh history before retrying"
  # Recorded for the operator's benefit only. The guard above deliberately does NOT read it: it asks
  # the ASG for every recent refresh and for the fleet's actual launch-template identity, so a
  # refresh started outside this kit is caught too and a stale record cannot make a run skip.
  state_put refresh_pending_lt_version "$target_refresh_lt" \
    || die "refresh $refresh_id started on $ASG but recording its target version failed; inspect the group's refresh history before retrying"
  say "controlled instance refresh started: $refresh_id"
  # Deliberately NOT pinned with --desired-configuration: that would rewrite the group's configuration
  # on success and could clobber a concurrent repoint. The consequence is a real, remaining window --
  # instances replaced later in this refresh follow the group's configuration AS IT IS THEN, so
  # repointing the group or moving $Default while this runs can converge part of the fleet elsewhere.
  echo "   NOTE do not repoint $ASG or move the launch template's \$Default while this refresh runs;"
  echo "        later replacements follow the group's configuration at that moment, not this target"
  echo "   NEXT: bash lib/apply-restorepatch.sh verify --env \"$ENVJSON\" --kit \"$KITDIR\""
  ;;

verify)
  rc=0
  verify_pass_count=0
  verify_fail_count=0
  verify_skip_count=0
  verify_status() {
    local kind="$1" message="$2"
    echo "   $kind $message"
    case "$kind" in
      PASS) verify_pass_count=$((verify_pass_count + 1)) ;;
      FAIL|INCONCLUSIVE) verify_fail_count=$((verify_fail_count + 1)) ;;
      SKIP|ABSENT) verify_skip_count=$((verify_skip_count + 1)) ;;
    esac
  }
  verify_out_of_scope() {
    verify_status SKIP "$1 (out of scope: $VERIFY_SCOPE)"
  }
  print_optional_verify_value() {
    case "$1" in
      ""|None) echo "   <absent>" ;;
      *) printf '%s\n' "$1" ;;
    esac
  }

  if scope_includes data; then
    say "hook heartbeat"
    hook_backup_timeout="$(state_get hook_backup_timeout)"
    if [ -z "$hook_backup_timeout" ]; then
      verify_status SKIP "heartbeat assertion: this run did not apply the hook concern"
    else
      cur="$(aws_ autoscaling describe-lifecycle-hooks --auto-scaling-group-name "$ASG" \
        --query "LifecycleHooks[?LifecycleHookName=='${HOOK_NAME}'].HeartbeatTimeout|[0]" --output text)"
      if [ "$cur" = "$NEW_TIMEOUT" ]; then
        verify_status PASS "heartbeat=$cur"
      else
        verify_status FAIL "heartbeat=$cur"
        rc=1
      fi
    fi

    say "MinSize must be unchanged (this tool never sets it to zero)"
    minsz="$(aws_ autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
      --query 'AutoScalingGroups[0].MinSize' --output text)"
    backup_minsz="$(state_get asg_backup_min_size)"
    if [ -z "$backup_minsz" ]; then
      verify_status SKIP "no backup MinSize record; run backup before verify"
    elif [ "$minsz" = "$backup_minsz" ]; then
      verify_status PASS "MinSize=$minsz unchanged from backup"
    else
      verify_status FAIL "this tool changed MinSize: backup=$backup_minsz current=$minsz"
      rc=1
    fi

    say "every S3 object the promoted bootstrap consumes still matches the kit"
    # Read-only replay of the gate the publish path runs, in a subshell so its die() ends the check
    # rather than verify. Nothing else in verify can see a partial or reverted publish: the objects
    # are fetched at boot, so the fleet reports it as the next instance ABANDONing, hours later.
    dep_report="$(mktemp)"
    if ( assert_bootstrap_script_deps "${KITDIR}/host-scripts" ) >"$dep_report" 2>&1; then
      verify_status PASS "$(grep -m1 'PASS ' "$dep_report" | sed 's/^ *//')"
    else
      verify_status FAIL "a boot dependency does not match the kit; a new host would ABANDON"
      sed 's/^/   /' "$dep_report"
      rc=1
    fi
    rm -f "$dep_report"

    if asg_lt_is_pinned; then
      say "ASG-referenced LT version $ASG_LT_REF carries the new bootstrap prefix and the new image"
      echo "   NOTE this ASG is pinned to version $ASG_LT_REF; the checks below judge that version,"
      echo "        not whatever the default alias points at"
    else
      say "default LT version carries the new bootstrap prefix and the new image"
    fi
    ud="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
      --versions "$ASG_LT_REF" --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' \
      --output text | base64 -d)"
    if printf '%s' "$ud" | grep -q "$ASSET_BUNDLE_PREFIX"; then
      verify_status PASS "bootstrap prefix"
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

# Same judgement as reconcile's B-s3 bootstrap classification above; change both together.
def code_only(p):
    # NOTE: this same judgement is implemented at four sites in this file
    # (resolve_bootstrap_state, reconcile_b_s3, and twice in verify). Change them
    # together — a divergent copy makes two gates disagree about one environment.
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
        verify_status PASS "bootstrap content-equal under a different prefix: $verify_prefix"
      else
        # resolve_bootstrap_state can fail closed into DRIFT during precheck; verify is an
        # acceptance path, so an undecidable download or render must count as a failure.
        verify_status FAIL "bootstrap prefix absent"
        rc=1
      fi
      [ -z "$verify_probe" ] || rm -rf "$verify_probe"
    fi
    if printf '%s' "$ud" | grep -q '{{'; then
      verify_status FAIL "unrendered placeholder in UserData"
      rc=1
    else
      verify_status PASS "no unrendered placeholder"
    fi
    img="$(aws_ ec2 describe-launch-template-versions --launch-template-id "$LT_ID" \
      --versions "$ASG_LT_REF" --query 'LaunchTemplateVersions[0].LaunchTemplateData.ImageId' --output text)"
    if [ -n "$AMI" ]; then
      if [ "$img" = "$AMI" ]; then
        verify_status PASS "ImageId=$img"
      else
        verify_status FAIL "ImageId=$img"
        rc=1
      fi
    else
      verify_status INCONCLUSIVE "new_ami_id absent from environment; ImageId=$img"
      rc=1
    fi

    say "bootstrap object present at its content-addressed key"
    expected_bootstrap_sha="$(state_get rendered_bootstrap_sha256)"
    bootstrap_object_prefix="$(printf '%s' "$ud" \
      | grep -oE 'deployment/bootstrap/host/[0-9a-f]{64}' | head -1 | sed 's|.*/||')"
    bootstrap_readback="$(mktemp)"
    bootstrap_rendered=""
    if [ -z "$bootstrap_object_prefix" ]; then
      verify_status FAIL "cannot extract bootstrap object prefix from default LT UserData"
      rc=1
    elif aws_ s3 cp "s3://${BUCKET}/deployment/bootstrap/host/${bootstrap_object_prefix}/init-host.sh" \
        "$bootstrap_readback" --no-progress >/dev/null 2>&1; then
      actual_bootstrap_sha="$(sha256_file "$bootstrap_readback")"
      if [ -n "$expected_bootstrap_sha" ] && [ "$actual_bootstrap_sha" = "$expected_bootstrap_sha" ]; then
        verify_status PASS "object present with this apply's rendered sha256=$actual_bootstrap_sha"
      elif [ -n "$expected_bootstrap_sha" ]; then
        verify_status FAIL "bootstrap sha256=$actual_bootstrap_sha expected=$expected_bootstrap_sha"
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
    # NOTE: this same judgement is implemented at four sites in this file
    # (resolve_bootstrap_state, reconcile_b_s3, and twice in verify). Change them
    # together — a divergent copy makes two gates disagree about one environment.
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
            verify_status PASS "object content-equal to kit artifact at live prefix: $bootstrap_object_prefix"
          else
            verify_status FAIL "bootstrap content differs from kit artifact at live prefix: $bootstrap_object_prefix"
            rc=1
          fi
        else
          verify_status FAIL "cannot render kit bootstrap from live object at prefix: $bootstrap_object_prefix"
          rc=1
        fi
      fi
      # Comments retain historical placeholders, so only executable lines are gated.
      if awk '!/^[[:space:]]*#/ && /\{\{[A-Z_]+\}\}/ {found=1} END {exit found}' "$bootstrap_readback"; then
        verify_status PASS "bootstrap code lines have no unresolved placeholder"
      else
        verify_status FAIL "bootstrap code line contains an unresolved placeholder"
        rc=1
      fi
    else
      verify_status FAIL "object missing"
      rc=1
    fi
    rm -f "$bootstrap_readback"
    [ -z "$bootstrap_rendered" ] || rm -f "$bootstrap_rendered"

    say "instance refresh progress"
    refresh_progress="$(aws_ autoscaling describe-instance-refreshes --auto-scaling-group-name "$ASG" \
      --query 'InstanceRefreshes[0].[Status,PercentageComplete]' --output text)"
    print_optional_verify_value "$refresh_progress"

    say "CodeBuild functional config unchanged (content-hash churn only)"
    functional_config="$(aws_ codebuild batch-get-projects --names openclaw-golden-image-builder \
      --query 'projects[0].[environment.type,environment.computeType,timeoutInMinutes]' --output text)"
    print_optional_verify_value "$functional_config"
  else
    say "hook heartbeat"
    verify_out_of_scope "heartbeat assertion"
    say "MinSize must be unchanged (this tool never sets it to zero)"
    verify_out_of_scope "MinSize assertion"
    say "default LT version carries the new bootstrap prefix and the new image"
    verify_out_of_scope "default LT bootstrap prefix assertion"
    verify_out_of_scope "default LT UserData placeholder assertion"
    verify_out_of_scope "default LT ImageId assertion"
    say "bootstrap object present at its content-addressed key"
    verify_out_of_scope "bootstrap object content assertion"
    verify_out_of_scope "bootstrap object placeholder assertion"
    say "instance refresh progress"
    verify_out_of_scope "instance refresh assertion"
    say "CodeBuild functional config unchanged (content-hash churn only)"
    verify_out_of_scope "image-build functional-config assertion"
  fi

  if scope_includes control; then
    say "openclaw-api code changed and its environment was not overwritten"
    want="$(state_get api_env_key_count)"
    now="$(aws_ lambda get-function-configuration --function-name "$FN" \
      --query 'length(Environment.Variables)' --output text)"
    if [ -n "$want" ]; then
      if [ "$want" = "$now" ]; then
        verify_status PASS "env keys=$now"
      else
        verify_status FAIL "env keys $want -> $now"
        rc=1
      fi
    fi
    invoke_event="$(mktemp)"
    cat > "$invoke_event" <<'JSON'
{
  "httpMethod": "GET",
  "resource": "/hosts",
  "path": "/hosts",
  "pathParameters": null,
  "queryStringParameters": null,
  "headers": {},
  "body": null
}
JSON
    if invoke_function_error="$(aws_ lambda invoke --function-name "$FN" \
        --payload "fileb://${invoke_event}" /tmp/restorepatch-invoke.json \
        --query FunctionError --output text)"; then
      if printf '%s\n' "$invoke_function_error" | grep -qi '^none$'; then
        verify_status PASS "invoke has no FunctionError; inspect /tmp/restorepatch-invoke.json"
      else
        verify_status FAIL "invoke FunctionError=${invoke_function_error:-unknown}; inspect /tmp/restorepatch-invoke.json"
        rc=1
      fi
    else
      verify_status FAIL "invoke command failed; inspect /tmp/restorepatch-invoke.json"
      rc=1
    fi
    rm -f "$invoke_event"
    if [ -f /tmp/restorepatch-invoke.json ] \
        && ! grep -Eqi '"(errorType|errorMessage)"[[:space:]]*:|Traceback' \
          /tmp/restorepatch-invoke.json; then
      verify_status PASS "invoke response has no errorType/errorMessage/Traceback; inspect /tmp/restorepatch-invoke.json"
    else
      verify_status FAIL "invoke response contains an unhandled-exception envelope or is missing; inspect /tmp/restorepatch-invoke.json"
      rc=1
    fi

    say "discovered API-package peers carry the overlay byte for byte"
    if [ "$PEER_DISCOVERY_CONFIRMED" != "True" ] \
        && [ "$PEER_DISCOVERY_CONFIRMED" != "true" ]; then
      verify_status FAIL "API-package peer discovery is unconfirmed"
      rc=1
    elif [ -z "$PEER_FNS" ]; then
      verify_status PASS "no additional API-package peers were discovered"
    else
      for peer_fn in $PEER_FNS; do
        peer_verify_work="$(mktemp -d)"
        peer_verify_loc="$(aws_ lambda get-function --function-name "$peer_fn" \
          --query 'Code.Location' --output text)" || peer_verify_loc=""
        if [ -z "$peer_verify_loc" ] \
            || ! curl -fsS -o "${peer_verify_work}/live.zip" "$peer_verify_loc" \
            || ! (cd "$peer_verify_work" && unzip -oq live.zip); then
          verify_status FAIL "function=$peer_fn package could not be inspected"
          rc=1
          rm -rf "$peer_verify_work"
          break
        fi
        peer_mismatch=""
        while IFS= read -r rel; do
          case "$rel" in
            __pycache__/*|*/__pycache__/*|*.pyc|*.pyo|.DS_Store|*/.DS_Store)
              continue
              ;;
          esac
          if [ ! -f "${peer_verify_work}/${rel}" ] \
              || ! cmp -s "${KITDIR}/lambda/api/${rel}" "${peer_verify_work}/${rel}"; then
            peer_mismatch="$rel"
            break
          fi
        done < <(cd "${KITDIR}/lambda/api" && find . -type f -print | sed 's|^\./||' | sort)
        if [ -n "$peer_mismatch" ]; then
          verify_status FAIL "function=$peer_fn module=$peer_mismatch differs from the kit"
          rc=1
          rm -rf "$peer_verify_work"
          break
        fi
        verify_status PASS "function=$peer_fn every overlaid module matches the kit byte for byte"
        rm -rf "$peer_verify_work"
      done
    fi

    say "API unqualified and alias-resolved code paths have converged"
    applied_version="$(state_get api_applied_version)"
    applied_alias="$(state_get api_alias_name)"
    if [ -n "$applied_alias" ]; then
      alias_version="$(aws_ lambda get-alias --function-name "$FN" --name "$applied_alias" \
        --query FunctionVersion --output text)"
      if [ -z "$applied_version" ]; then
        # No version published by a run that is still in force — either this run skipped
        # apply because the overlay was already in service, or a rollback cleared the
        # record. Comparing the live alias against an empty expectation reported FAIL for
        # a healthy environment (measured: apply → rollback → verify). Skip only the
        # version comparison; the CodeSha256 convergence check below still runs and is
        # what actually proves both serving paths agree.
        verify_status SKIP "alias version comparison: this run published no version (live alias -> $alias_version)"
      elif [ "$alias_version" = "$applied_version" ]; then
        verify_status PASS "alias $applied_alias points to version $applied_version"
      else
        verify_status FAIL "alias $applied_alias points to $alias_version, expected $applied_version"
        rc=1
      fi
      latest_sha="$(aws_ lambda get-function-configuration --function-name "$FN" \
        --query CodeSha256 --output text)"
      alias_sha="$(aws_ lambda get-function-configuration --function-name "$FN" \
        --qualifier "$alias_version" --query CodeSha256 --output text)"
      if [ "$latest_sha" = "$alias_sha" ]; then
        verify_status PASS "unqualified and alias-resolved CodeSha256 match"
      else
        verify_status FAIL "CodeSha256 differs between unqualified and alias-resolved paths"
        rc=1
      fi
    else
      verify_status ABSENT "alias path; unqualified version is the serving path"
    fi
  else
    say "openclaw-api code changed and its environment was not overwritten"
    verify_out_of_scope "API environment-key preservation assertion"
    verify_out_of_scope "API invocation assertion"
    say "discovered API-package peers carry the overlay byte for byte"
    verify_out_of_scope "API-package peer overlay assertion"
    say "API unqualified and alias-resolved code paths have converged"
    verify_out_of_scope "API alias-version assertion"
    verify_out_of_scope "API unqualified/alias CodeSha256 assertion"
  fi

  echo "verify scope=$VERIFY_SCOPE pass=$verify_pass_count fail=$verify_fail_count skip=$verify_skip_count"
  [ "$rc" -eq 0 ] && say "verify PASS" || say "verify FAIL"
  exit "$rc"
  ;;

rollback)
  [ -f "$STATE" ] || die "no backup state; cannot roll back safely"
  hb="$(state_get hook_backup_timeout)"
  lv="$(state_get host_lt_backup_version)"
  av="$(state_get api_backup_version)"
  [ -n "$hb" ] && [ -n "$lv" ] && [ -n "$av" ] || die "backup state incomplete"
  [ -n "$(state_get hook_backup_default_result)" ] \
    || die "backup state predates DefaultResult capture; re-run backup before rolling back"

  hook_dr="$(state_get hook_backup_default_result)"
  [ -n "$hook_dr" ] || die "backup state has no hook_backup_default_result; cannot restore the hook faithfully"
  say "restore hook heartbeat to $hb and DefaultResult to $hook_dr"
  aws_ autoscaling put-lifecycle-hook --auto-scaling-group-name "$ASG" \
    --lifecycle-hook-name "$HOOK_NAME" \
    --lifecycle-transition autoscaling:EC2_INSTANCE_LAUNCHING \
    --heartbeat-timeout "$hb" --default-result "$hook_dr" || die "hook restore failed"

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
  for peer_fn in $PEER_FNS; do
    [ -n "$(state_get "backup_${peer_fn}")" ] && restore_function "$peer_fn"
  done
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

  # The applied-version record describes what THIS apply published. After a rollback it
  # is no longer true, and `verify` compares the live alias against it — so leaving it
  # behind makes the next verify fail with "alias live points to <old>, expected <rolled
  # back>" while the code itself is fine (the CodeSha256 assertion right after it
  # passes). Clear it so a post-rollback verify judges the restored state, not a version
  # this tool just undid. Measured on a live environment: apply → rollback → verify.
  state_put api_applied_version ""
  echo "   cleared the applied-version record (it described the rolled-back apply)"

  backup_desired="$(state_get asg_backup_desired)"
  if [ -n "$backup_desired" ]; then
    # Canary capacity is temporary and rollback must converge to the pre-apply desired value.
    aws_ autoscaling set-desired-capacity --auto-scaling-group-name "$ASG" \
      --desired-capacity "$backup_desired" --honor-cooldown \
      || die "cannot restore desired capacity to $backup_desired"
  fi
  state_put canary_pass false
  if [ -n "$(state_get host_lt_new_version)" ]; then
    say "roll the fleet back onto the restored template"
    aws_ autoscaling start-instance-refresh --auto-scaling-group-name "$ASG" \
      --preferences '{"MinHealthyPercentage":100,"MaxHealthyPercentage":100,"InstanceWarmup":900,"SkipMatching":false}' \
      --query InstanceRefreshId --output text \
      || die "cannot start rollback refresh"
  else
    echo "   SKIP fleet refresh: no data-plane concern was applied"
  fi
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
  if [ -z "$expected_deployment" ]; then
    echo "   SKIP stage deployment assertion: apply-api has not recorded a replacement deployment"
  elif [ "$stage_deployment" = "$expected_deployment" ]; then
    echo "   PASS stage $API_STAGE deploymentId=$stage_deployment"
  else
    echo "   FAIL stage $API_STAGE deploymentId=$stage_deployment expected=$expected_deployment"
    rc=1
  fi
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
