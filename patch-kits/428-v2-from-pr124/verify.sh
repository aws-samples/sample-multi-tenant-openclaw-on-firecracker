#!/usr/bin/env bash
set -euo pipefail
KIT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${CLAW_PATCH_RUN_DIR:-$KIT/run}"
exec "${PYTHON:-python3}" "$KIT/handlers/patchctl.py" verify --run-dir "$RUN_DIR"
