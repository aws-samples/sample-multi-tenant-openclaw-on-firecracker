#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Drive a SET of sibling kits with a ledger, so "the patch is applied" is a claim about all of
# them and not about whichever one ran last.
#
# The problem this solves: a rollup is split into one kit per resource (each needs its own
# canary, approval and rollback). Run them by hand and a failure on kit 2 leaves kit 1 applied
# and kit 3 untouched — a mixed version with nothing recording that fact. The ledger makes the
# partial state explicit and refuses to call the set complete until every member verifies.
#
# Order matters and is taken from the directory list you pass, left to right. Put the kit
# others depend on first; the run stops at the first failure rather than continuing past it.
#
# Usage:
#   patch-set.sh apply    <environment.json> <answers-dir> <kit> [<kit> ...]
#   patch-set.sh verify   <environment.json> <kit> [<kit> ...]
#   patch-set.sh rollback <environment.json> <kit> [<kit> ...]   # reverse order
#   patch-set.sh status   <environment.json> <kit> [<kit> ...]
set -euo pipefail

# No braces in the message: `${1:?...}` text goes through brace expansion, so a literal
# {apply|verify} would be expanded and end up assigned to MODE.
MODE="${1:?usage: patch-set.sh apply|verify|rollback|status <environment.json> ...}"
ENVJSON="${2:?environment.json required}"
shift 2
ANSWERS_DIR=""
if [[ "$MODE" == "apply" ]]; then
  ANSWERS_DIR="${1:?answers dir required for apply}"
  shift
fi
[[ $# -gt 0 ]] || { echo "at least one kit directory required" >&2; exit 2; }
KITS=("$@")

HERE="$(cd "$(dirname "$0")" && pwd)"
LEDGER="${OC_PATCH_SET_LEDGER:-$(pwd)/patch-set-ledger.json}"
RECEIPT_DIR="${OC_PATCH_RECEIPT_DIR:-$(dirname "$LEDGER")/patch-receipts}"

kit_id() { jq -r '.id' "$1/manifest.json"; }
kit_ver() { jq -er '.kit_fingerprint' "$1/REVIEW.json"; }
kit_runtime() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys
print((Path(sys.argv[1]) / "runtime" / "scripts").resolve())
PY
}
runtime_fingerprint() {
  python3 - "$1" <<'PY'
from pathlib import Path, PurePosixPath
import hashlib
import json
import sys

kit = Path(sys.argv[1])
runtime = kit / "runtime"
scripts = runtime / "scripts"
for directory in (runtime, scripts):
    if directory.is_symlink() or not directory.is_dir():
        raise SystemExit(f"invalid packaged runtime directory: {directory}")

manifest = json.loads((kit / "manifest.json").read_text(encoding="utf-8"))
inventory = manifest.get("kit_files")
if not isinstance(inventory, dict):
    raise SystemExit("manifest.kit_files is not an object")
declared = {}
for relative, record in inventory.items():
    if not isinstance(relative, str) or not relative.startswith("runtime/scripts/"):
        continue
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise SystemExit(f"unsafe runtime inventory path: {relative}")
    if not isinstance(record, dict) or not isinstance(record.get("sha256"), str):
        raise SystemExit(f"invalid runtime inventory record: {relative}")
    declared[relative] = record["sha256"]
if not declared:
    raise SystemExit("no packaged runtime inventory")

actual = {}
for path in sorted(scripts.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"runtime symlink is not allowed: {path}")
    if path.is_file():
        relative = path.relative_to(kit).as_posix()
        actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
if actual != declared:
    raise SystemExit("runtime files do not match manifest.kit_files")
encoded = json.dumps(sorted(actual.items()), separators=(",", ":")).encode()
print(hashlib.sha256(encoded).hexdigest())
PY
}

FIRST_RUNTIME="$(kit_runtime "${KITS[0]}")"
if [[ "$HERE" != "$FIRST_RUNTIME" ]]; then
  echo "PATCH_SET_FAILED: invoke ${KITS[0]}/runtime/scripts/patch-set.sh, not $0" >&2
  exit 2
fi
EXPECTED_RUNTIME="$(runtime_fingerprint "${KITS[0]}")" || {
  echo "PATCH_SET_FAILED: first kit has no valid packaged runtime inventory" >&2
  exit 2
}
for k in "${KITS[@]}"; do
  current_runtime="$(runtime_fingerprint "$k")" || {
    echo "PATCH_SET_FAILED: $k has no valid packaged runtime inventory" >&2
    exit 2
  }
  if [[ "$current_runtime" != "$EXPECTED_RUNTIME" ]]; then
    echo "PATCH_SET_FAILED: sibling kit runtime differs: $k" >&2
    exit 2
  fi
done

require_current_review() {
  local k="$1" output rc runtime
  runtime="$(kit_runtime "$k")"
  set +e
  output="$(python3 "$runtime/review-kit.py" check "$k" 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    printf 'KIT REVIEW FAILED for %s:\n%s\n' "$k" "$output" >&2
    return "$rc"
  fi
}

# Every mode either executes generated kit bytes or trusts a ledger claim about them. Validate all
# receipts before reading the ledger so status/verify/rollback cannot bypass the same gate as apply.
for k in "${KITS[@]}"; do
  require_current_review "$k"
done

# The ledger entry must be bound to the ENVIRONMENT too, not just the kit. Keyed by kit id and
# reviewed fingerprint alone, a ledger carried to another account/region would report
# SET_COMPLETE for resources that were never touched there.
ENV_FINGERPRINT="$(jq -r '[(.account|tostring), .region,
  (.asg.name // "-"), ((.hosts.instance_ids // []) | sort | join(","))] | join("|")' "$ENVJSON" \
  | sha256sum | awk '{print $1}')"
ENV_SHA256="$(sha256sum "$ENVJSON" | awk '{print $1}')"

# The ledger is keyed by kit id AND reviewed fingerprint, so regenerating a kit at the same source
# patch cannot inherit completion or idempotency evidence from different generated bytes.
ledger_get() {
  [[ -f "$LEDGER" ]] || { printf 'ABSENT'; return; }
  jq -r --arg k "$1" --arg v "$2" --arg e "$ENV_FINGERPRINT" \
    '.kits[$k] // {} | if (.version == $v and .env == $e) then (.state // "ABSENT") else "ABSENT" end' \
    "$LEDGER" 2>/dev/null || printf 'ABSENT'
}

ledger_set() {
  local kid="$1" ver="$2" state="$3" tmp
  [[ -f "$LEDGER" ]] || printf '{"kits":{}}' > "$LEDGER"
  tmp="${LEDGER}.tmp.$$"
  jq --arg k "$kid" --arg v "$ver" --arg s "$state" --arg e "$ENV_FINGERPRINT" \
    '.kits[$k] = {version: $v, env: $e, state: $s}' "$LEDGER" > "$tmp"
  mv -f "$tmp" "$LEDGER"
}

# Lane resolution lives in _lanes.sh so adding a lane does not mean editing four drivers and
# hoping none was missed. A driver that does not know a lane does not fail loudly — it treats the
# kit as host-config and hunts for a host snapshot the kit does not have.
# shellcheck source=_lanes.sh
source "$HERE/_lanes.sh"
kit_entry() { oc_kit_entry "$1"; }
kit_needs_envjson() { oc_lane_needs_envjson "$1"; }

run_kit_stage() {
  local k="$1" stage="$2" entry
  entry="$(kit_entry "$k")"
  if kit_needs_envjson "$k"; then
    bash "$entry/$stage.sh" "$ENVJSON"
  else
    OC_PATCH_REGION="$(jq -r '.region' "$ENVJSON")" \
    OC_PATCH_ACCOUNT="$(jq -r '.account|tostring' "$ENVJSON")" \
      bash "$entry/$stage.sh"
  fi
}

run_kit_verify() { run_kit_stage "$1" verify; }

run_kit_rollback() { run_kit_stage "$1" rollback; }

new_receipt_path() {
  local kid="$1"
  mkdir -p "$RECEIPT_DIR"
  mktemp "$RECEIPT_DIR/${kid}.XXXXXX"
}

run_autopatch() {
  local k="$1" ans="$2" receipt="$3" runtime
  runtime="$(kit_runtime "$k")"
  bash "$runtime/autopatch.sh" "$k" "$ENVJSON" --answers "$ans" --receipt "$receipt"
}

validate_receipt_binding() {
  local k="$1" receipt="$2" expected="${3:-}"
  [[ -s "$receipt" ]] || {
    echo "RECEIPT MISSING: fixed driver did not write $receipt" >&2
    return 23
  }
  jq -e --arg f "$(kit_ver "$k")" --arg e "$ENV_SHA256" --arg r "$expected" '
      .schema_version == 1
      and .kit_fingerprint == $f
      and .environment_sha256 == $e
      and (.writes | type == "number")
      and ($r == "" or .result == $r)
    ' "$receipt" >/dev/null || {
    echo "RECEIPT INVALID: result or kit/environment binding does not match" >&2
    return 23
  }
}

# A first clean apply proves only that the happy path worked once. Run the whole fixed driver
# again and demand the generated apply's explicit SKIP. This is the operational idempotency
# receipt: SET_COMPLETE is not printed until a real retry converges without replaying the write.
prove_idempotency() {
  local k="$1" ans="$2" kid receipt rc
  kid="$(kit_id "$k")"
  receipt="$(new_receipt_path "$kid")"
  set +e
  run_autopatch "$k" "$ans" "$receipt"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    echo "IDEMPOTENCY FAILED: second fixed-driver run returned rc=$rc" >&2
    return "$rc"
  fi
  validate_receipt_binding "$k" "$receipt" SKIP || return $?
  jq -e '.writes == 0' "$receipt" >/dev/null || {
    echo "IDEMPOTENCY UNPROVEN: second receipt did not report writes=0" >&2
    return 23
  }
  echo "IDEMPOTENCY_VERIFIED: second run returned result=SKIP writes=0 receipt=$receipt"
}

say() { printf '\n--- %s ---\n' "$*"; }

case "$MODE" in
  status)
    printf '%-34s %-14s %s\n' KIT STATE VERSION
    for k in "${KITS[@]}"; do
      printf '%-34s %-14s %s\n' "$(kit_id "$k")" "$(ledger_get "$(kit_id "$k")" "$(kit_ver "$k")")" \
        "$(kit_ver "$k" | cut -c1-12)"
    done
    exit 0
    ;;

  apply)
    for k in "${KITS[@]}"; do
      kid="$(kit_id "$k")"; ver="$(kit_ver "$k")"
      state="$(ledger_get "$kid" "$ver")"
      ans="$ANSWERS_DIR/$kid.json"
      [[ -f "$ans" ]] || { echo "no answers file $ans" >&2; ledger_set "$kid" "$ver" FAILED; exit 3; }
      if [[ "$state" == "IDEMPOTENCY_VERIFIED" ]]; then
        # Trust nothing on the strength of the ledger alone: it records what we DID, and the
        # resource can have drifted since. Re-observe before skipping.
        say "RECHECK $kid (ledger says IDEMPOTENCY_VERIFIED — confirming it still holds)"
        set +e
        run_kit_verify "$k" >/dev/null 2>&1
        rc=$?
        set -e
        if [[ "$rc" -eq 0 ]]; then
          say "SKIP $kid (verified live, not just in the ledger)"
          continue
        fi
        say "ledger said IDEMPOTENCY_VERIFIED but live verify returned $rc — re-applying"
        ledger_set "$kid" "$ver" "DRIFTED_rc${rc}"
      fi

      # A legacy VERIFIED/APPLIED_ONCE ledger means the resource was applied but no real second
      # apply was recorded. Upgrade it by running the idempotency pass instead of trusting it.
      if [[ "$state" != "VERIFIED" && "$state" != "APPLIED_ONCE" ]]; then
        say "APPLY $kid"
        receipt="$(new_receipt_path "$kid")"
        # IN_PROGRESS is written BEFORE the work, so a crash leaves the set visibly mid-flight
        # rather than looking untouched.
        ledger_set "$kid" "$ver" IN_PROGRESS
        if run_autopatch "$k" "$ans" "$receipt" &&
            validate_receipt_binding "$k" "$receipt"; then
          ledger_set "$kid" "$ver" APPLIED_ONCE
        else
          rc=$?
          ledger_set "$kid" "$ver" "FAILED_rc${rc}"
          echo >&2
          echo "SET_INCOMPLETE: $kid failed (rc=$rc). Later kits were NOT attempted." >&2
          echo "  ledger: $LEDGER" >&2
          echo "  Fix or roll back before continuing; the ledger records what is applied." >&2
          exit "$rc"
        fi
      fi

      say "IDEMPOTENCY $kid (real second apply must SKIP)"
      if prove_idempotency "$k" "$ans"; then
        ledger_set "$kid" "$ver" IDEMPOTENCY_VERIFIED
      else
        rc=$?
        ledger_set "$kid" "$ver" "IDEMPOTENCY_FAILED_rc${rc}"
        echo "SET_INCOMPLETE: $kid applied once but retry convergence was not proven." >&2
        exit "$rc"
      fi
    done
    ;;

  verify)
    for k in "${KITS[@]}"; do
      kid="$(kit_id "$k")"; ver="$(kit_ver "$k")"
      before="$(ledger_get "$kid" "$ver")"
      say "VERIFY $kid"
      # Re-observe rather than trust the ledger: the ledger says what we did, the kit's verify
      # says what is true now.
      # Capture rc BEFORE touching the ledger. `cmd || { ledger_set ...; exit $?; }` exits
      # with ledger_set's status (0), which turned a DRIFT into a clean exit — the worst
      # possible bug in the one place an unattended driver looks.
      set +e
      run_kit_verify "$k"
      rc=$?
      set -e
      if [[ "$rc" -ne 0 ]]; then
        ledger_set "$kid" "$ver" "DRIFTED_rc${rc}"
        echo "VERIFY FAILED for $kid (rc=$rc)" >&2
        exit "$rc"
      fi
      if [[ "$before" == "IDEMPOTENCY_VERIFIED" ]]; then
        ledger_set "$kid" "$ver" IDEMPOTENCY_VERIFIED
      else
        ledger_set "$kid" "$ver" VERIFIED
      fi
    done
    ;;

  rollback)
    # Reverse order: undo dependents before what they depend on.
    for ((i=${#KITS[@]}-1; i>=0; i--)); do
      k="${KITS[$i]}"; kid="$(kit_id "$k")"; ver="$(kit_ver "$k")"
      state="$(ledger_get "$kid" "$ver")"
      if [[ "$state" == "ABSENT" ]]; then
        say "SKIP $kid (nothing recorded for this version)"
        continue
      fi
      say "ROLLBACK $kid (was $state)"
      set +e
      run_kit_rollback "$k"
      rc=$?
      set -e
      if [[ "$rc" -eq 0 ]]; then
        ledger_set "$kid" "$ver" ROLLED_BACK
      else
        ledger_set "$kid" "$ver" "ROLLBACK_FAILED_rc${rc}"
        echo "ROLLBACK FAILED for $kid (rc=$rc); earlier kits were NOT rolled back" >&2
        exit "$rc"
      fi
    done
    # Rollback has its OWN success condition. Falling through to the VERIFIED check reported a
    # fully successful rollback as SET_INCOMPLETE.
    echo
    echo "SET_ROLLED_BACK ${#KITS[@]}/${#KITS[@]} kit(s) reverted"
    exit 0
    ;;

  *) echo "unknown mode $MODE" >&2; exit 2 ;;
esac

# The set is complete only when EVERY member is VERIFIED for this exact version. Anything else
# prints the outstanding members and exits non-zero, because a partially-applied set is the
# state most likely to be mistaken for done.
outstanding=()
for k in "${KITS[@]}"; do
  state="$(ledger_get "$(kit_id "$k")" "$(kit_ver "$k")")"
  if [[ "$MODE" == "apply" ]]; then
    [[ "$state" == "IDEMPOTENCY_VERIFIED" ]] ||
      outstanding+=("$(kit_id "$k"):$state")
  else
    [[ "$state" == "VERIFIED" || "$state" == "IDEMPOTENCY_VERIFIED" ]] ||
      outstanding+=("$(kit_id "$k"):$state")
  fi
done
echo
if [[ "${#outstanding[@]}" -eq 0 ]]; then
  if [[ "$MODE" == "apply" ]]; then
    echo "SET_COMPLETE ${#KITS[@]}/${#KITS[@]} kit(s) applied, verified, and retry-proven"
  else
    echo "SET_VERIFIED ${#KITS[@]}/${#KITS[@]} kit(s) live"
  fi
  exit 0
fi
echo "SET_INCOMPLETE ${#outstanding[@]} of ${#KITS[@]} kit(s) not verified:" >&2
printf '  %s\n' "${outstanding[@]}" >&2
exit 22
