#!/usr/bin/env bash
# Main-stack deploy wrapper: live tenant-query preflight must pass before CDK.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${1:?usage: deploy-cdk.sh <region> <profile|-> [cdk deploy args...]}"
PROFILE="${2:?usage: deploy-cdk.sh <region> <profile|-> [cdk deploy args...]}"
shift 2

CDK_BIN="${CDK_BIN:-cdk}"

_python_ready() {
  [ -n "${1:-}" ] && [ -x "$1" ] &&
    "$1" -c "import boto3, yaml" >/dev/null 2>&1
}

if [ -n "${PYTHON_BIN:-}" ]; then
  if ! _python_ready "$PYTHON_BIN"; then
    echo "ERROR: PYTHON_BIN cannot import boto3 and yaml: $PYTHON_BIN" >&2
    exit 2
  fi
elif _python_ready "$ROOT/.venv/bin/python"; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
elif _python_ready "$(command -v python3 2>/dev/null || true)"; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "ERROR: no Python interpreter can import boto3 and yaml" >&2
  exit 2
fi

PREFLIGHT_ARGS=(
  "$ROOT/scripts/checks/tenant-query-rollout.py"
  --config "$ROOT/config.yml"
  --region "$REGION"
)
if [ "$PROFILE" != "-" ]; then
  PREFLIGHT_ARGS+=(--profile "$PROFILE")
fi

"$PYTHON_BIN" "${PREFLIGHT_ARGS[@]}"
exec "$CDK_BIN" deploy "$@"
