#!/usr/bin/env bash
# Apply an exact API Gateway route-resource spec through the stateful Python helper.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/apply-api-routes.py" "$@"
