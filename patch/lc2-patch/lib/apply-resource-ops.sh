#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Per-resource apply / verify / rollback for the CloudFormation changes this kit computed,
# WITHOUT ever updating a stack.
#
# This is a thin front for lib/oc_resource_ops.py, which holds the logic. The split is
# deliberate: closure parsing, expected-value extraction and readback assertions are all
# structured comparisons, and doing them in shell is how a check quietly degrades into a
# grep that passes on the wrong thing.
#
# Contract every operation follows:
#   gate -> mutate -> read the live resource back -> assert it equals the value the CLOSURE
#   expects -> only then append a receipt line.
# The expected value never comes from the live resource, a failed assertion runs that
# operation's own restore before exiting non-zero, and `verify` re-reads the live resource
# rather than reading the receipt. The receipt is therefore evidence, not an assertion.
#
# It refuses an unknown op id rather than deriving a call for an arbitrary resource type:
# AWS::IAM::Policy and AWS::CodeBuild::Project need different calls, and inventing one from
# a template is how a live system breaks. Use `apply-cfn-resources.sh plan` to READ the rest
# of the closure.
#
# Usage: apply-resource-ops.sh <op-id> {apply|verify|rollback|gate|plan} <closure-dir> <region> [<diff>]
#   op ids: iam-edge-putmetricdata | lambda-api-code | lambda-api-alias | ssm-deadline-params
#           | s3-edge-bundle | s3-obs-assets | codebuild-golden-image
#           | cw-drop-replication-lag-alarms | host-init-bootstrap
#   gate is available only for iam-edge-putmetricdata; s3-edge-bundle apply calls it itself.
#   <diff> is REQUIRED by host-init-bootstrap plan/verify (lib/init-host.sh.diff) and rejected
#   for every other op, so a stray argument cannot be silently ignored.
#
# Required environment, per op (each one fails closed with the variable name):
#   OC_RUN_ID                     unique per apply run; rollback restores THIS run's state
#   iam / s3-edge-bundle          EDGE_ROLE_ARN EDGE_ROLE_NAME ASSETS_BUCKET
#   lambda-api-code               OPENCLAW_API_FN OVERLAY_ZIP BACKUP_S3_BUCKET BACKUP_S3_KEY
#   lambda-api-alias              OPENCLAW_API_FN OPENCLAW_API_ALIAS
#   codebuild-golden-image        GOLDEN_IMAGE_PROJECT GOLDEN_IMAGE_ROLE_ARN
#                                 CDK_ASSETS_BUCKET REPO_SOURCE_ZIP
#   cw-drop-replication-lag-...   REDIS_REPLICATION_GROUP_ID EDGE_ASG
#     (the reader-endpoint parameter name comes from the closure, not from the environment)
set -euo pipefail

usage() {
  sed -n '2,40p' "$0" >&2
  exit 2
}

[ "$#" -eq 4 ] || [ "$#" -eq 5 ] || usage
OP="$1"; MODE="$2"; CLOSURE="$3"; REGION="$4"; DIFF="${5:-}"

case "$MODE" in apply|verify|rollback|gate|plan) ;; *) echo "unknown mode $MODE" >&2; usage ;; esac

# The diff argument is checked HERE, not just forwarded: host-init-bootstrap apply/verify cannot
# run without it, and any other op receiving one means the caller confused two operations.
if [ "$OP" = "host-init-bootstrap" ]; then
  case "$MODE" in
    plan|verify)
      [ -n "$DIFF" ] || { echo "FATAL: $OP $MODE requires the template diff path (lib/init-host.sh.diff)" >&2; exit 2; }
      [ -f "$DIFF" ] || { echo "FATAL: template diff not found: $DIFF" >&2; exit 2; } ;;
    *)
      [ -z "$DIFF" ] || { echo "FATAL: $OP $MODE takes no diff argument" >&2; exit 2; } ;;
  esac
elif [ -n "$DIFF" ]; then
  echo "FATAL: only host-init-bootstrap accepts a fifth argument; got '$DIFF' for $OP" >&2
  exit 2
fi
[ -d "$CLOSURE" ] || { echo "FATAL: no closure dir $CLOSURE" >&2; exit 2; }
for t in aws python3; do
  command -v "$t" >/dev/null || { echo "FATAL: need $t on PATH" >&2; exit 2; }
done

HERE="$(cd "$(dirname "$0")" && pwd)"
IMPL="$HERE/oc_resource_ops.py"
[ -f "$IMPL" ] || { echo "FATAL: oc_resource_ops.py not found next to apply-resource-ops.sh" >&2; exit 2; }

if [ -n "$DIFF" ]; then
  exec python3 "$IMPL" "$OP" "$MODE" "$CLOSURE" "$REGION" "$DIFF"
fi
exec python3 "$IMPL" "$OP" "$MODE" "$CLOSURE" "$REGION"
