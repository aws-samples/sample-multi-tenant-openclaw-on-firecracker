#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# overlay-lambda.sh — overlay this kit's first-party Lambda source onto a LIVE function's package
# WITHOUT a stack update, preserving the live package's compiled (arm64 native) dependencies.
#
# Why overlay (not a fresh zip): the api asset carries arm64 native wheels installed at bundle time.
# Freezing our own dep versions onto it is an unrequested change and can break the runtime. So we
# download the live zip, overwrite only files shipped by this changed-file kit, re-zip,
# update-function-code, and publish-version as the rollback anchor.
#
# Both openclaw-api AND openclaw-lifecycle-consumer are built from the SAME deploy/lambda/api asset
# (deploy/stacks/lambdas.py), so BOTH must be overlaid or the queued lifecycle path keeps stale code.
#
# Usage:  overlay-lambda.sh apply   <function-name> <kit-source-dir> <region> [alias]
#         overlay-lambda.sh verify  <function-name> <region> [alias]
#         overlay-lambda.sh rollback <function-name> <backup-zip> <region> [alias]
#   <kit-source-dir>  a dir in this kit whose contents overlay the function root (e.g. lambda/api).
#   The kit source is a partial changed-file tree; unrelated live package files are preserved.
#   alias: when provided, publish the patched code and move exactly this alias with RevisionId CAS.
set -euo pipefail
CMD="${1:?usage: overlay-lambda.sh <apply|verify|rollback> ...}"

fn_codesha() {
  local qualifier_args=()
  [ -z "${3:-}" ] || qualifier_args=(--qualifier "$3")
  aws lambda get-function --function-name "$1" "${qualifier_args[@]}" --region "$2" \
    --query 'Configuration.CodeSha256' --output text
}

safe_alias() {
  [ -z "$1" ] || [[ "$1" =~ ^[A-Za-z0-9_-]+$ ]] || {
    echo "FATAL: unsafe alias name: $1" >&2
    exit 2
  }
}

case "$CMD" in
apply)
  FN="${2:?function name required}"
  SRC="${3:?kit source dir required}"
  REGION="${4:?region required}"
  ALIAS="${5:-}"
  safe_alias "$ALIAS"
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
  if [ -n "$ALIAS" ]; then
    aws lambda get-alias --function-name "$FN" --name "$ALIAS" --region "$REGION" \
      --output json >"$work/live.zip.alias.json"
  fi
  ( cd "$work" && mkdir unz && cd unz && unzip -q ../live.zip )

  # This kit contains only changed/added files, not complete core/services trees.
  cp -a "$SRC"/. "$work/unz/"
  ( cd "$work/unz" && zip -qr ../new.zip . )

  revision_id="$(aws lambda get-function-configuration --function-name "$FN" \
    --region "$REGION" --query RevisionId --output text)"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file "fileb://$work/new.zip" --revision-id "$revision_id" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  if [ -n "$ALIAS" ]; then
    target_version="$(aws lambda publish-version --function-name "$FN" --region "$REGION" \
      --query Version --output text)"
    alias_revision="$(jq -r .RevisionId "$work/live.zip.alias.json")"
    printf '%s\n' "$target_version" >"$work/live.zip.target-version"
    aws lambda update-alias --function-name "$FN" --name "$ALIAS" \
      --function-version "$target_version" --revision-id "$alias_revision" \
      --region "$REGION" >/dev/null
    echo "PASS: moved $FN:$ALIAS to patched version $target_version"
  fi
  echo "PASS: overlaid $FN (new CodeSha256=$(fn_codesha "$FN" "$REGION" "$ALIAS")); backup=$(cat "$work/backup-version.txt")"
  echo "$work/live.zip"   # emit the backup zip path for the operator's rollback anchor
  ;;

verify)
  FN="${2:?function name required}"
  REGION="${3:?region required}"
  ALIAS="${4:-}"
  safe_alias "$ALIAS"
  qualifier_args=()
  [ -z "$ALIAS" ] || qualifier_args=(--qualifier "$ALIAS")
  # REST API v1 dry invoke: imports must succeed; the unknown resource may return a normal 4xx.
  out="$(mktemp)"
  aws lambda invoke --function-name "$FN" "${qualifier_args[@]}" --region "$REGION" \
    --payload '{"httpMethod":"GET","resource":"/__patch_import_probe","path":"/__patch_import_probe","headers":{},"requestContext":{"identity":{}}}' \
    --cli-binary-format raw-in-base64-out "$out" \
    --query 'FunctionError' --output text >"$out.err" 2>/dev/null || true
  ferr="$(cat "$out.err" 2>/dev/null || echo None)"
  if [ "$ferr" = "None" ] || [ -z "$ferr" ]; then
    echo "PASS: $FN${ALIAS:+:$ALIAS} invoke returned FunctionError=None (new modules import cleanly), CodeSha256=$(fn_codesha "$FN" "$REGION" "$ALIAS")"
  else
    echo "FAIL: $FN${ALIAS:+:$ALIAS} invoke FunctionError=$ferr — a new module failed to import" >&2
    exit 1
  fi
  ;;

rollback)
  FN="${2:?function name required}"
  BACKUP="${3:?backup zip path required}"
  REGION="${4:?region required}"
  ALIAS="${5:-}"
  safe_alias "$ALIAS"
  [ -f "$BACKUP" ] || { echo "FATAL: backup zip not found: $BACKUP" >&2; exit 1; }
  restore_alias=false
  if [ -n "$ALIAS" ]; then
    alias_state="$BACKUP.alias.json"
    target_state="$BACKUP.target-version"
    [ -f "$alias_state" ] && [ -f "$target_state" ] || {
      echo "FATAL: alias rollback metadata is missing beside $BACKUP" >&2
      exit 1
    }
    current_alias="$(aws lambda get-alias --function-name "$FN" --name "$ALIAS" \
      --region "$REGION" --output json)"
    current_version="$(printf '%s' "$current_alias" | jq -r .FunctionVersion)"
    current_routing="$(printf '%s' "$current_alias" | jq -c '.RoutingConfig // {"AdditionalVersionWeights":{}}')"
    original_version="$(jq -r .FunctionVersion "$alias_state")"
    original_routing="$(jq -c '.RoutingConfig // {"AdditionalVersionWeights":{}}' "$alias_state")"
    target_version="$(cat "$target_state")"
    [ "$current_routing" = "$original_routing" ] || {
      echo "FATAL: $FN:$ALIAS routing drifted after apply" >&2
      exit 1
    }
    case "$current_version" in
      "$target_version") restore_alias=true ;;
      "$original_version") restore_alias=false ;;
      *)
        echo "FATAL: $FN:$ALIAS drifted to $current_version; expected $target_version or original $original_version" >&2
        exit 1
        ;;
    esac
  fi
  echo "[overlay:$FN] REDEPLOY_ZIP rollback from $BACKUP"
  revision_id="$(aws lambda get-function-configuration --function-name "$FN" \
    --region "$REGION" --query RevisionId --output text)"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file "fileb://$BACKUP" --revision-id "$revision_id" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  if [ "$restore_alias" = true ]; then
    alias_revision="$(aws lambda get-alias --function-name "$FN" --name "$ALIAS" \
      --region "$REGION" --query RevisionId --output text)"
    aws lambda update-alias --function-name "$FN" --name "$ALIAS" \
      --function-version "$original_version" --revision-id "$alias_revision" \
      --routing-config "$original_routing" --region "$REGION" >/dev/null
    echo "PASS: restored $FN:$ALIAS to version $original_version"
  fi
  echo "PASS: restored $FN from backup (CodeSha256=$(fn_codesha "$FN" "$REGION" "$ALIAS"))"
  ;;

*)
  echo "usage: overlay-lambda.sh <apply|verify|rollback> ..." >&2
  exit 2
  ;;
esac
