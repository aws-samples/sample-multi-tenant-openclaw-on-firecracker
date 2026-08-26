#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Front end for lib/apply_egress_207.py. Checks the arguments and the per-operation environment
# BEFORE the Python starts, so a missing variable is one clear message instead of a traceback
# halfway through a mutation.
#
# Usage: apply-egress-207.sh <operation> <apply|verify|rollback> <region>
#
# Environment, per operation:
#   lambda-api-code   OPENCLAW_API_FN  BACKUP_S3_BUCKET  BACKUP_S3_KEY  [OPENCLAW_API_ALIAS]
#                     apply needs all of them. The backup bucket must have versioning enabled: the
#                     unwind restores $LATEST from a pinned version, and a mutable key cannot be
#                     pinned. Supply OPENCLAW_API_ALIAS to move the alias as well — the API Gateway
#                     invokes the alias while the dispatch event-source mapping binds $LATEST, so
#                     leaving it out patches only the dispatch path.
#   edge-bundle       ASSETS_BUCKET  EDGE_ASG
#                     The destination prefix is discovered from the launch template the ASG pins, so
#                     it is never supplied by hand.
#
# Always:  OC_RUN_ID (unique per apply run; rollback needs the same value)
# Optional: OC_WORK_DIR (defaults to a temp dir — never inside the kit, which would make the kit
#           fail its own validator), OC_RECEIPT_FILE
set -euo pipefail

OP="${1:-}"; MODE="${2:-}"; REGION="${3:-}"
if [ -z "$OP" ] || [ -z "$MODE" ] || [ -z "$REGION" ]; then
  echo "usage: $0 <lambda-api-code|edge-bundle> <apply|verify|rollback> <region>" >&2
  exit 2
fi
case "$OP" in lambda-api-code|edge-bundle) ;; *) echo "unknown operation: $OP" >&2; exit 2 ;; esac
case "$MODE" in apply|verify|rollback) ;; *) echo "unknown mode: $MODE" >&2; exit 2 ;; esac

need() {
  for v in "$@"; do
    eval "val=\${$v:-}"
    if [ -z "$val" ]; then
      echo "environment variable $v is required for $OP/$MODE" >&2
      exit 2
    fi
  done
}

need OC_RUN_ID
case "$OP" in
  lambda-api-code)
    need OPENCLAW_API_FN
    [ "$MODE" = "apply" ] && need BACKUP_S3_BUCKET BACKUP_S3_KEY
    ;;
  edge-bundle)
    need ASSETS_BUCKET EDGE_ASG
    ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/apply_egress_207.py" "$OP" "$MODE" "$REGION"
