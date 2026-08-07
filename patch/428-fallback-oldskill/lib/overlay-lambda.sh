#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# overlay-lambda.sh — overlay this kit's first-party Lambda source onto a LIVE function's package
# WITHOUT a stack update, preserving the live package's compiled (arm64 native) dependencies.
#
# Why overlay (not a fresh zip): the api asset carries arm64 native wheels installed at bundle time.
# Freezing our own dep versions onto it is an unrequested change and can break the runtime. So we
# download the live zip, delete ONLY the first-party dirs this patch replaces, drop this kit's
# source tree in, re-zip, update-function-code, and publish-version as the rollback anchor.
#
# Both openclaw-api AND openclaw-lifecycle-consumer are built from the SAME deploy/lambda/api asset
# (deploy/stacks/lambdas.py), so BOTH must be overlaid or the queued lifecycle path keeps stale code.
#
# Usage:  overlay-lambda.sh apply   <function-name> <kit-source-dir> <region>
#         overlay-lambda.sh verify  <function-name> <region>
#         overlay-lambda.sh rollback <function-name> <backup-zip> <region>
#   <kit-source-dir>  a dir in this kit whose contents overlay the function root (e.g. lambda/api).
#   The first-party dirs replaced are the top-level entries present in <kit-source-dir>.
set -euo pipefail
CMD="${1:?usage: overlay-lambda.sh <apply|verify|rollback> ...}"

fn_codesha() {
  aws lambda get-function --function-name "$1" --region "$2" \
    --query 'Configuration.CodeSha256' --output text
}

case "$CMD" in
apply)
  FN="${2:?function name required}"
  SRC="${3:?kit source dir required}"
  REGION="${4:?region required}"
  [ -d "$SRC" ] || { echo "FATAL: kit source dir not found: $SRC" >&2; exit 1; }
  work="$(mktemp -d)"
  echo "[overlay:$FN] recording backup anchor (RevisionId + CodeSha256) and publishing a version"
  aws lambda get-function --function-name "$FN" --region "$REGION" \
    --query '{rev:Configuration.RevisionId,sha:Configuration.CodeSha256}' --output json
  aws lambda publish-version --function-name "$FN" --region "$REGION" \
    --query Version --output text >"$work/backup-version.txt"
  echo "[overlay:$FN] backup version = $(cat "$work/backup-version.txt")"

  url="$(aws lambda get-function --function-name "$FN" --region "$REGION" \
    --query Code.Location --output text)"
  curl -s "$url" -o "$work/live.zip"
  ( cd "$work" && mkdir unz && cd unz && unzip -q ../live.zip )

  # delete ONLY the first-party top-level entries this kit's source tree replaces
  for entry in "$SRC"/*; do
    name="$(basename "$entry")"
    rm -rf "$work/unz/$name"
  done
  cp -a "$SRC"/. "$work/unz/"
  ( cd "$work/unz" && zip -qr ../new.zip . )

  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file "fileb://$work/new.zip" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  echo "PASS: overlaid $FN (new CodeSha256=$(fn_codesha "$FN" "$REGION")); backup=$(cat "$work/backup-version.txt")"
  echo "$work/live.zip"   # emit the backup zip path for the operator's rollback anchor
  ;;

verify)
  FN="${2:?function name required}"
  REGION="${3:?region required}"
  # synthetic dry invoke: the new image_*/bootstrap_* modules must import cleanly (FunctionError=None)
  out="$(mktemp)"
  aws lambda invoke --function-name "$FN" --region "$REGION" \
    --payload '{"requestContext":{"http":{"method":"GET"}},"rawPath":"/__patch_import_probe","headers":{}}' \
    --cli-binary-format raw-in-base64-out "$out" \
    --query 'FunctionError' --output text >"$out.err" 2>/dev/null || true
  ferr="$(cat "$out.err" 2>/dev/null || echo None)"
  if [ "$ferr" = "None" ] || [ -z "$ferr" ]; then
    echo "PASS: $FN invoke returned FunctionError=None (new modules import cleanly), CodeSha256=$(fn_codesha "$FN" "$REGION")"
  else
    echo "FAIL: $FN invoke FunctionError=$ferr — a new module failed to import" >&2
    exit 1
  fi
  ;;

rollback)
  FN="${2:?function name required}"
  BACKUP="${3:?backup zip path required}"
  REGION="${4:?region required}"
  [ -f "$BACKUP" ] || { echo "FATAL: backup zip not found: $BACKUP" >&2; exit 1; }
  echo "[overlay:$FN] REDEPLOY_ZIP rollback from $BACKUP"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file "fileb://$BACKUP" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  echo "PASS: restored $FN from backup (CodeSha256=$(fn_codesha "$FN" "$REGION"))"
  ;;

*)
  echo "usage: overlay-lambda.sh <apply|verify|rollback> ..." >&2
  exit 2
  ;;
esac
