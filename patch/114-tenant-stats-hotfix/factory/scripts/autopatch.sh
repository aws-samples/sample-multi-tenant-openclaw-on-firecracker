#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# End-to-end unattended patch driver. One command, fixed order, fail-closed at every step:
#
#   preflight (read-only)  ->  interview (once, answered up front)  ->  verify-first
#   ->  apply (canary -> verify -> approval -> fleet -> future source)  ->  final verify
#
# The point is that a human decides ONCE, before anything is touched, and after that the run
# either completes or stops on a machine gate. It never pauses to ask something it could have
# asked at the start, and it never reports success it did not observe.
#
# On any failure after the first write, it prints the exact rollback command and stops. It
# does NOT auto-roll-back: an operator deciding to revert a partially-applied fleet is a
# judgement call, and the rollback itself needs the same lease and gates.
#
# Usage: autopatch.sh <kit-dir> <environment.json>
#                     [--answers <answers.json>] [--receipt <receipt.json>]
set -euo pipefail

printf '%s\n' \
  "PATCH_114_FACTORY_DISABLED: refusing to generate or execute this hotfix." \
  "The factory is incomplete: it omits the tenant-stats writer Lambda, writer IAM and environment, the EventBridge schedule, and an authenticated HTTP end-to-end test." \
  "Its route hard-codes authorization_type=NONE and can bypass the platform CUSTOM authorizer in platform-key mode." \
  "Do not use previously generated kits. Replace the factory with a complete, authenticated, end-to-end-verified patch before re-enabling it." >&2
exit 78

KIT="${1:?usage: autopatch.sh <kit-dir> <environment.json> [--answers answers.json]}"
ENVJSON="${2:?usage: autopatch.sh <kit-dir> <environment.json> [--answers answers.json]}"
shift 2
ANSWERS=""
RECEIPT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --answers) ANSWERS="${2:?--answers needs a path}"; shift 2 ;;
    --receipt) RECEIPT="${2:?--receipt needs a path}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
step() { printf '\n=== %s ===\n' "$*"; }
die() { printf 'STOPPED: %s\n' "$*" >&2; exit "${2:-1}"; }
PREVERIFY="$(mktemp "${TMPDIR:-/tmp}/oc-preverify.XXXXXXXX")"
preserve_preverify() {
  local rc=$? trash target
  trap - EXIT
  set +e
  if [[ -e "$PREVERIFY" ]]; then
    trash="${HOME:-/tmp}/Documents/trashllm/oc-patch-temp"
    mkdir -p "$trash"
    target="$trash/$(basename "$PREVERIFY").$$.${RANDOM}"
    mv "$PREVERIFY" "$target"
  fi
  exit "$rc"
}
trap preserve_preverify EXIT

emit_receipt() {
  local result="$1" writes="$2" fingerprint environment_sha payload temporary
  fingerprint="$(jq -er '.kit_fingerprint' "$KIT/REVIEW.json")"
  environment_sha="$(sha256sum "$ENVJSON" | awk '{print $1}')"
  payload="$(python3 - "$result" "$writes" "$fingerprint" "$environment_sha" \
      "$ARTIFACT_ID" "$CONTENT_VERSION" <<'PYEOF'
import datetime
import json
import sys

result, writes, fingerprint, environment_sha, artifact_id, content_version = sys.argv[1:]
print(json.dumps({
    "schema_version": 1,
    "result": result,
    # This is the number of generated apply phases entered by this driver run.
    # SKIP=0 is exact; nonzero is deliberately conservative about internal AWS calls.
    "writes": int(writes),
    "kit_fingerprint": fingerprint,
    "environment_sha256": environment_sha,
    "artifact_id": artifact_id,
    "content_version": content_version,
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, sort_keys=True, separators=(",", ":")))
PYEOF
  )"
  if [[ -n "$RECEIPT" ]]; then
    mkdir -p "$(dirname "$RECEIPT")"
    temporary="${RECEIPT}.tmp.$$"
    printf '%s\n' "$payload" > "$temporary"
    mv -f "$temporary" "$RECEIPT"
  fi
  printf 'AUTOPATCH_RECEIPT %s\n' "$payload"
}

step "STEP 1/6  preflight (read-only, one time)"
bash "$HERE/preflight-once.sh" "$KIT" "$ENVJSON" ||
  die "preflight failed — nothing was changed" 10

step "STEP 2/6  plan (read-only: what changes, permissions, conflicts, canary)"
# terraform separates plan from apply so a human approves a SPECIFIC plan. Regenerating the
# plan here would refresh the very hashes the stale-plan check compares, making that check
# structurally incapable of failing — which is exactly what happened before this guard.
# So: an existing plan is REUSED and validated; one is generated only when none exists, and in
# that case the run stops so the operator can actually read it before anything is written.
if [[ -f "$KIT/PLAN.json" ]]; then
  echo "reusing the plan on file (generated earlier, approved by whoever ran plan)"
  want_m="$(jq -r '.manifest_sha256' "$KIT/PLAN.json")"
  want_e="$(jq -r '.environment_sha256' "$KIT/PLAN.json")"
  now_m="$(sha256sum "$KIT/manifest.json" | awk '{print $1}')"
  now_e="$(sha256sum "$ENVJSON" | awk '{print $1}')"
  if [[ "$want_m" != "$now_m" || "$want_e" != "$now_e" ]]; then
    echo "  manifest: ${want_m:0:12} -> ${now_m:0:12}" >&2
    echo "  environment: ${want_e:0:12} -> ${now_e:0:12}" >&2
    die "STALE PLAN: inputs changed after the plan was made; re-run patch-plan.sh" 32
  fi
  PLAN_RC="$(jq -r 'if .conflicts > 0 then 30 elif .unknowns > 0 then 31 else 0 end' \
    "$KIT/PLAN.json")"
else
  echo "no plan on file — generating one, then stopping so it can be read"
  set +e
  bash "$HERE/patch-plan.sh" "$KIT" "$ENVJSON"
  gen=$?
  set -e
  [[ "$gen" -le 31 ]] || die "plan failed (rc=$gen)" "$gen"
  die "plan written to $KIT/PLAN.json — review it, then rerun to apply it" 33
fi
case "$PLAN_RC" in
  0)  ;;
  30) die "plan found conflicts — resolve them before apply" 30 ;;
  31) # An unknown is not a conflict, but proceeding means accepting that the plan could not
      # check something. Require an explicit opt-in rather than deciding for the operator.
      [[ "${OC_PATCH_ACCEPT_UNKNOWNS:-}" == "yes" ]] ||
        die "plan is incomplete; rerun with OC_PATCH_ACCEPT_UNKNOWNS=yes to proceed anyway" 31
      echo "proceeding with unknowns, explicitly accepted" ;;
  *)  die "plan failed (rc=$PLAN_RC)" "$PLAN_RC" ;;
esac

step "STEP 3/6  interview (every decision, asked once)"
if [[ -n "$ANSWERS" ]]; then
  python3 "$HERE/interview-once.py" record "$KIT" "$ANSWERS" ||
    die "answers rejected — see the reason above" 11
fi
if ! python3 "$HERE/interview-once.py" check "$KIT"; then
  echo
  echo "No valid decision on file. The questions this patch requires:" >&2
  python3 "$HERE/interview-once.py" ask "$KIT" >&2
  die "answer the questions above, then rerun with --answers <file>" 11
fi

# Every gate below is bound to THIS kit at THIS content version, so a decision recorded for a
# different patch cannot widen this one.
# Two entrypoint layouts. A host-config kit has lib/compiled/{apply,verify,rollback,clean}.sh
# driven by recipe.json; a control-plane kit has lib/compiled/<lane-id>/{apply,verify,rollback}
# and no recipe.json. Which lanes exist is defined once, in _lanes.sh.
# shellcheck source=_lanes.sh
source "$HERE/_lanes.sh"
KIND="$(oc_kit_lane "$KIT")"
ENTRY="$(oc_kit_entry "$KIT")"
if [[ "$KIND" == "host-config" ]]; then
  ARTIFACT_ID="$(jq -r '.artifact_id' "$KIT/lib/compiled/recipe.json")"
  CONTENT_VERSION="$(jq -r '.content_version' "$KIT/lib/compiled/recipe.json")"
else
  ARTIFACT_ID="$(jq -r '.id' "$KIT/manifest.json")"
  CONTENT_VERSION="$(jq -r '.patch_sha' "$KIT/manifest.json")"
  # A control-plane kit needs the region in the env; there is no host snapshot to read it from.
  export OC_PATCH_REGION="${OC_PATCH_REGION:-$(jq -r '.region' "$ENVJSON")}"
  export OC_PATCH_ACCOUNT="${OC_PATCH_ACCOUNT:-$(jq -r '.account | tostring' "$ENVJSON")}"
fi
echo "kit lane: $KIND"
WIDEN="$(jq -r '.fleet_widening // "hold"' "$KIT/DECISION.json")"

# A host-config stage takes environment.json (it needs the host snapshot and the lease
# bucket); a control-plane stage takes none (its target is one function, resolved from the
# env vars exported above). Keep the difference in one place so every call site is identical.
STAGE_ARGS=()
[[ "$KIND" == "host-config" ]] && STAGE_ARGS=("$ENVJSON")
run_stage() {
  bash "$ENTRY/$1.sh" ${STAGE_ARGS[@]+"${STAGE_ARGS[@]}"}
}

step "STEP 4/6  pre-apply verify (data plane checked BEFORE it is touched)"
# A verify against an unpatched fleet is EXPECTED to fail (there is no patch-owned anchor
# yet) — its job is to prove the probe machinery works and to record the pre-state, not to
# pass. Exit 44 (no anchor) is the healthy answer here; a crash is not.
set +e
run_stage verify > "$PREVERIFY" 2>&1
PRE=$?
set -e
case "$PRE" in
  0)  echo "already applied and live — skipping the write phase"
      emit_receipt SKIP 0
      echo
      echo "AUTOPATCH_COMPLETE kit=$ARTIFACT_ID version=$CONTENT_VERSION"
      echo "  Verified before apply. No generated apply stage was entered."
      exit 0 ;;
  44) echo "no complete patch-owned anchor yet; apply may start or resume safely" ;;
  40) sed -n '1,20p' "$PREVERIFY" >&2
      die "the live file matches neither base nor patch — someone else changed it (DRIFT)" 40 ;;
  *)  sed -n '1,20p' "$PREVERIFY" >&2
      die "pre-apply verify stopped the run (rc=$PRE)" "$PRE" ;;
esac

step "STEP 5/6  apply (canary -> verify -> approval -> fleet -> future source)"
APPLY_ENV=()
if [[ "$WIDEN" == "pre-approve" ]]; then
  echo "fleet widening was pre-approved in the interview"
  APPLY_ENV+=("OC_PATCH_FLEET_APPROVED=${ARTIFACT_ID}:${CONTENT_VERSION}")
else
  echo "fleet widening was HELD — the run will stop after the canary for a look"
fi
set +e
env "${APPLY_ENV[@]}" bash "$ENTRY/apply.sh" ${STAGE_ARGS[@]+"${STAGE_ARGS[@]}"}
RC=$?
set -e
case "$RC" in
  0)  echo "apply reported a clean sweep" ;;
  20) echo
      echo "HELD at the fleet gate: the canary is patched and verified, the rest is not."
      echo "Look at the canary, then continue with:"
      echo "  OC_PATCH_FLEET_APPROVED=${ARTIFACT_ID}:${CONTENT_VERSION} \\"
      echo "    bash $ENTRY/apply.sh ${STAGE_ARGS[*]-}"
      echo "Rerun is safe: patched hosts SKIP."
      exit 20 ;;
  21) echo
      echo "Snapshot hosts are patched and verified, but FLEET COVERAGE IS UNPROVEN"
      echo "(see the FLEET_DRIFT line above). Resolve it, then rerun."
      exit 21 ;;
  *)  echo
      echo "APPLY FAILED (rc=$RC). Nothing further will be attempted." >&2
      echo "To revert what this run did:" >&2
      echo "  bash $ENTRY/rollback.sh ${STAGE_ARGS[*]-}" >&2
      echo "Rollback is drift-guarded: it refuses if the live file is no longer" >&2
      echo "patch-owned, so it cannot clobber someone else's change." >&2
      exit "$RC" ;;
esac

step "STEP 6/6  post-apply verify (independent re-observation)"
# Deliberately a SECOND, standalone verify: apply's internal gate ran inside the lease, this
# one runs fresh with no lease and no state writes. If the effect only held transiently
# during apply, this is what catches it.
set +e
run_stage verify
POST=$?
set -e
if [[ "$POST" -ne 0 ]]; then
  echo
  echo "POST-VERIFY FAILED (rc=$POST) — the change is installed but not observably live." >&2
  echo "  bash $ENTRY/rollback.sh ${STAGE_ARGS[*]-}" >&2
  exit "$POST"
fi

echo
echo "AUTOPATCH_COMPLETE kit=$ARTIFACT_ID version=$CONTENT_VERSION"
echo "  Verified independently after apply. Rerunning this command is a no-op."
emit_receipt APPLIED 1
