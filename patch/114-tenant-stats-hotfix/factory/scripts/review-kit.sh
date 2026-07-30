#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

KIT="${1:?usage: review-kit.sh <kit-dir> <rubric-file>}"
RUBRIC="${2:?usage: review-kit.sh <kit-dir> <rubric-file>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -d "$KIT" && -f "$RUBRIC" ]] || {
  echo "REVIEW_FAILED: kit or rubric is missing" >&2
  exit 2
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/oc-patch-claude-review.XXXXXXXX")"
cleanup() {
  local rc=$? trash
  trap - EXIT
  trash="${HOME}/Documents/trashllm/oc-patch-review"
  mkdir -p "$trash"
  mv "$WORK" "$trash/$(basename "$WORK").$$"
  exit "$rc"
}
trap cleanup EXIT

MATERIAL="$WORK/material.txt"
PROMPT="$WORK/prompt.txt"
VERDICT="$WORK/verdict.txt"
python3 "$HERE/review-kit.py" prepare "$KIT" "$MATERIAL"
{
  printf '%s\n\n' \
    "Act as an independent production hot-patch reviewer." \
    "Do not use tools and do not trust the generator's own claims." \
    "Read every shipped byte below. Follow the rubric exactly." \
    "If any blocker exists, state one BLOCKER: line per defect and end with a BLOCK verdict." \
    "A passing answer must end with the exact KIT_REVIEW_VERDICT line requested by the material."
  printf '%s\n\n' "TASK RUBRIC" "============"
  sed -n '1,10000p' "$RUBRIC"
  printf '%s\n\n' "CANONICAL KIT MATERIAL" "======================"
  sed -n '1,1000000p' "$MATERIAL"
} > "$PROMPT"

claude --safe-mode --print --model opus --effort xhigh --tools "" \
  --no-session-persistence < "$PROMPT" > "$VERDICT"

OC_PATCH_REVIEW_WRAPPER=claude \
  python3 "$HERE/review-kit.py" _seal "$KIT" "$MATERIAL" "$VERDICT"
python3 "$HERE/review-kit.py" check "$KIT"
echo "CLAUDE_REVIEW_COMPLETE kit=$KIT"
