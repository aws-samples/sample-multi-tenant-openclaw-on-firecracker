#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# The single mechanical generation entry point. It chooses exactly one typed lane
# from manifest data, compiles it, then packages the fixed runtime into the kit.
set -euo pipefail

printf '%s\n' \
  "PATCH_114_FACTORY_DISABLED: refusing to generate or execute this hotfix." \
  "The factory is incomplete: it omits the tenant-stats writer Lambda, writer IAM and environment, the EventBridge schedule, and an authenticated HTTP end-to-end test." \
  "Its route hard-codes authorization_type=NONE and can bypass the platform CUSTOM authorizer in platform-key mode." \
  "Do not use previously generated kits. Replace the factory with a complete, authenticated, end-to-end-verified patch before re-enabling it." >&2
exit 78

HERE="$(cd "$(dirname "$0")" && pwd)"
KIT="${1:?usage: compile-kit.sh <patch-kit> <source-repo>}"
REPO="${2:?usage: compile-kit.sh <patch-kit> <source-repo>}"
MANIFEST="$KIT/manifest.json"

[[ -f "$MANIFEST" ]] || {
  echo "COMPILE_KIT_FAILED: missing $MANIFEST" >&2
  exit 2
}
[[ -d "$REPO/.git" || -f "$REPO/.git" ]] || {
  echo "COMPILE_KIT_FAILED: source repo is not a Git worktree: $REPO" >&2
  exit 2
}

LANE_JSON="$(jq -cer '
  [
    .lambda_functions // [],
    .ddb_settings // [],
    .ddb_tables // [],
    .api_routes // []
  ] as $fields
  | if all($fields[]; type == "array") then
      [
        if ($fields[0] | length) > 0 then "lambda" else empty end,
        if ($fields[1] | length) > 0 then "ddb" else empty end,
        if ($fields[2] | length) > 0 then "ddbnew" else empty end,
        if ($fields[3] | length) > 0 then "apigw" else empty end
      ]
    else
      error("typed lane fields must be arrays")
    end
' "$MANIFEST")" || {
  echo "COMPILE_KIT_FAILED: manifest lane fields are invalid" >&2
  exit 2
}

LANES=()
while IFS= read -r lane; do
  [[ -n "$lane" ]] && LANES[${#LANES[@]}]="$lane"
done <<EOF
$(printf '%s' "$LANE_JSON" | jq -r '.[]')
EOF

if [[ "${#LANES[@]}" -gt 1 ]]; then
  echo "COMPILE_KIT_FAILED: one kit may declare only one typed lane: ${LANES[*]}" >&2
  exit 2
fi

LANE="${LANES[0]:-host-config}"

# A compiler rerun is a fresh build, not an in-place overlay. Preserve stale
# output for recovery, then invalidate every receipt tied to the old bytes.
TRASH_ROOT="${HOME}/Documents/trashllm/oc-patch-compile"
TRASH_RUN="$TRASH_ROOT/$(date +%Y%m%dT%H%M%S)-$$"
STALE=(
  "$KIT/lib/compiled"
  "$KIT/runtime"
  "$KIT/PLAN.json"
  "$KIT/DECISION.json"
  "$KIT/REVIEW.json"
  "$KIT/CLAUDE-REVIEW.txt"
  "$KIT/CLAUDE.md"
  "$KIT/APPLY-INSTRUCTIONS.md"
)
for path in "${STALE[@]}"; do
  [[ -e "$path" || -L "$path" ]] || continue
  mkdir -p "$TRASH_RUN"
  mv "$path" "$TRASH_RUN/$(basename "$path")"
done

MANIFEST_TMP="$(mktemp "${MANIFEST}.compile.XXXXXX")"
if ! jq '.kit_files = {}' "$MANIFEST" > "$MANIFEST_TMP"; then
  mkdir -p "$TRASH_RUN"
  mv "$MANIFEST_TMP" "$TRASH_RUN/manifest-invalid.json"
  echo "COMPILE_KIT_FAILED: cannot reset manifest.kit_files" >&2
  exit 2
fi
mv "$MANIFEST_TMP" "$MANIFEST"

case "$LANE" in
  lambda) python3 "$HERE/_compile_lambda.py" "$KIT" "$REPO" ;;
  ddb)    python3 "$HERE/_compile_ddb.py" "$KIT" "$REPO" ;;
  ddbnew) python3 "$HERE/_compile_ddb_create.py" "$KIT" "$REPO" ;;
  apigw)  python3 "$HERE/_compile_apigw.py" "$KIT" "$REPO" ;;
  host-config) bash "$HERE/compile-recipe.sh" "$KIT" "$REPO" ;;
  *) echo "COMPILE_KIT_FAILED: unsupported lane $LANE" >&2; exit 2 ;;
esac

python3 "$HERE/package-kit-docs.py" "$KIT"
python3 "$HERE/package-kit-runtime.py" "$KIT"
echo "COMPILED_KIT lane=$LANE runtime=packaged"
