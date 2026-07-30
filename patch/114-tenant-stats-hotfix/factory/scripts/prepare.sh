#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
  echo "PREPARE_FAILED: bash 4+ is required; found $BASH_VERSION" >&2
  exit 3
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
PATCH_ROOT="$(cd "$HERE/../.." && pwd)"
REPO="$(git -C "$PATCH_ROOT" rev-parse --show-toplevel)"
REGION="${1:?usage: prepare.sh <region> <customer-config.yml> [environment.json] [--skip-review]}"
CUSTOMER_CONFIG="${2:?usage: prepare.sh <region> <customer-config.yml> [environment.json] [--skip-review]}"
shift 2
ENVIRONMENT=""
SKIP_REVIEW=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-review) SKIP_REVIEW=1; shift ;;
    *)
      [[ -z "$ENVIRONMENT" ]] || {
        echo "PREPARE_FAILED: only one environment.json may be supplied" >&2
        exit 2
      }
      ENVIRONMENT="$1"
      shift
      ;;
  esac
done

CUSTOMER_CONFIG="$(cd "$(dirname "$CUSTOMER_CONFIG")" && pwd)/$(basename "$CUSTOMER_CONFIG")"
[[ -f "$CUSTOMER_CONFIG" && ! -L "$CUSTOMER_CONFIG" ]] || {
  echo "PREPARE_FAILED: customer config is not a regular file: $CUSTOMER_CONFIG" >&2
  exit 2
}

for tool in bash git jq python3 sha256sum; do
  command -v "$tool" >/dev/null || {
    echo "PREPARE_FAILED: need $tool" >&2
    exit 2
  }
done

BASE_SHA="a547dc74fe25ea0219c804933c5a7da8af1e3b39"
PATCH_SHA="f8b9e14e5f456a24dc8fc597528a7b1b1540a9f3"
git -C "$REPO" cat-file -e "${BASE_SHA}^{commit}"
git -C "$REPO" cat-file -e "${PATCH_SHA}^{commit}"

OUTPUT="${OC_PATCH_BUILD_ROOT:-$REPO/build/patch-114-tenant-stats-hotfix}"
case "$OUTPUT" in
  ""|"/"|"$REPO"|"${HOME:-/__no_home__}")
    echo "PREPARE_FAILED: unsafe build root: $OUTPUT" >&2
    exit 2
    ;;
esac
if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
  TRASH="${HOME}/Documents/trashllm/patch-114-builds"
  mkdir -p "$TRASH"
  mv "$OUTPUT" "$TRASH/$(date +%Y%m%dT%H%M%S)-$$"
fi
mkdir -p "$OUTPUT"
CONFIG_FACTS="$OUTPUT/config-facts.json"
python3 "$HERE/read-customer-config.py" "$CUSTOMER_CONFIG" "$CONFIG_FACTS"

if [[ -z "$ENVIRONMENT" ]]; then
  ENVIRONMENT="$OUTPUT/environment.json"
  bash "$HERE/discover-env.sh" "$REGION" \
    "$HERE/../manifests/114-api-lambda.json" "$ENVIRONMENT"
else
  ENVIRONMENT="$(cd "$(dirname "$ENVIRONMENT")" && pwd)/$(basename "$ENVIRONMENT")"
  [[ -f "$ENVIRONMENT" ]] || {
    echo "PREPARE_FAILED: environment file not found: $ENVIRONMENT" >&2
    exit 2
  }
fi
jq -e --arg region "$REGION" '.region == $region' "$ENVIRONMENT" >/dev/null || {
  echo "PREPARE_FAILED: requested region does not match environment.json" >&2
  exit 3
}

KITS="$OUTPUT/kits"
MATERIALIZE_ARGS=(
  "$ENVIRONMENT"
  "$CONFIG_FACTS"
  "$HERE/../manifests/114-api-lambda.json"
  "$KITS"
)
if [[ -n "${OC_CONTROL_PLANE_STAGE:-}" ]]; then
  MATERIALIZE_ARGS+=(--stage "$OC_CONTROL_PLANE_STAGE")
fi
python3 "$HERE/materialize-patch.py" "${MATERIALIZE_ARGS[@]}"

ORDER=(114-tenant-stats-table 114-api-lambda 114-tenants-stats-route)
for name in "${ORDER[@]}"; do
  bash "$HERE/compile-kit.sh" "$KITS/$name" "$REPO"
done

for name in "${ORDER[@]}"; do
  if bad="$(grep -R -F -l $'\\ ' "$KITS/$name/lib/compiled")"; then
    echo "PREPARE_FAILED: malformed shell continuation in $name" >&2
    printf '  %s\n' "$bad" >&2
    exit 2
  else
    grep_rc=$?
    if [[ "$grep_rc" -ne 1 ]]; then
      echo "PREPARE_FAILED: could not scan shell continuations in $name" >&2
      exit 2
    fi
  fi
  while IFS= read -r script; do
    bash -n "$script"
  done < <(find "$KITS/$name" -type f -name '*.sh' -print | sort)
done

if [[ "$SKIP_REVIEW" == "0" ]]; then
  command -v claude >/dev/null || {
    echo "PREPARE_FAILED: Claude Code CLI is required for independent kit review" >&2
    exit 2
  }
  for name in "${ORDER[@]}"; do
    bash "$HERE/review-kit.sh" \
      "$KITS/$name" "$HERE/../rubrics/$name.txt"
  done
else
  echo "WARNING: independent Claude review skipped; apply will remain blocked" >&2
fi

printf '%s\n' \
  "PATCH_SET_READY=$KITS" \
  "ENVIRONMENT=$ENVIRONMENT" \
  "ORDER=${ORDER[*]}"
