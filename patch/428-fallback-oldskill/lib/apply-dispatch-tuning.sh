#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apply-dispatch-tuning.sh — apply the lifecycle dispatch-consumer tuning this sync (a63d7b05) adds,
# WITHOUT a stack update (deploy/stacks/lambdas.py). Long image/rebuild flows overran the old 180s
# consumer timeout; the sync bumps:
#   * openclaw-lifecycle-consumer  Timeout 180 -> 900s
#   * openclaw-lifecycle.fifo      VisibilityTimeout 180 -> 960s   (must stay > function timeout)
#   * the consumer's SQS event-source-mapping  BatchSize 10 -> 1    (FIFO in-order, one msg/invoke)
# Rollback = RESTORE the three to 180/180/10.
#
# Usage:  apply-dispatch-tuning.sh <apply|verify|rollback> <region>
set -euo pipefail
CMD="${1:?usage: apply-dispatch-tuning.sh <apply|verify|rollback> <region>}"
REGION="${2:?region required}"
FN="openclaw-lifecycle-consumer"
QUEUE_NAME="openclaw-lifecycle.fifo"

queue_url() {
  aws sqs get-queue-url --queue-name "$QUEUE_NAME" --region "$REGION" \
    --query QueueUrl --output text
}

esm_uuid() {
  aws lambda list-event-source-mappings --function-name "$FN" --region "$REGION" \
    --query "EventSourceMappings[?contains(EventSourceArn,'$QUEUE_NAME')].UUID | [0]" \
    --output text
}

case "$CMD" in
apply)
  echo "[dispatch] consumer $FN Timeout -> 900"
  aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --timeout 900 >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"

  QURL="$(queue_url)"
  echo "[dispatch] queue $QUEUE_NAME VisibilityTimeout -> 960"
  aws sqs set-queue-attributes --queue-url "$QURL" --region "$REGION" \
    --attributes VisibilityTimeout=960 >/dev/null

  UUID="$(esm_uuid)"
  echo "[dispatch] consumer ESM $UUID BatchSize -> 1"
  aws lambda update-event-source-mapping --uuid "$UUID" --region "$REGION" \
    --batch-size 1 >/dev/null
  echo "PASS: dispatch tuned (Timeout=900, VisibilityTimeout=960, BatchSize=1)"
  ;;

verify)
  fail=0
  t="$(aws lambda get-function-configuration --function-name "$FN" --region "$REGION" \
    --query Timeout --output text)"
  [ "$t" = "900" ] && echo "PASS: $FN Timeout=900" || { echo "FAIL: $FN Timeout=$t (want 900)" >&2; fail=1; }
  QURL="$(queue_url)"
  vt="$(aws sqs get-queue-attributes --queue-url "$QURL" --region "$REGION" \
    --attribute-names VisibilityTimeout --query 'Attributes.VisibilityTimeout' --output text)"
  [ "$vt" = "960" ] && echo "PASS: $QUEUE_NAME VisibilityTimeout=960" || { echo "FAIL: VisibilityTimeout=$vt (want 960)" >&2; fail=1; }
  bs="$(aws lambda get-event-source-mapping --uuid "$(esm_uuid)" --region "$REGION" \
    --query BatchSize --output text)"
  [ "$bs" = "1" ] && echo "PASS: consumer ESM BatchSize=1" || { echo "FAIL: BatchSize=$bs (want 1)" >&2; fail=1; }
  # invariant: visibility must exceed function timeout, else in-flight msgs redeliver mid-run
  [ "$vt" -gt "$t" ] 2>/dev/null || { echo "FAIL: VisibilityTimeout($vt) must be > function Timeout($t)" >&2; fail=1; }
  [ "$fail" -eq 0 ] || exit 1
  echo "PASS: dispatch tuning present and consistent (960 > 900)"
  ;;

rollback)
  echo "[dispatch] RESTORE Timeout=180 / VisibilityTimeout=180 / BatchSize=10"
  aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --timeout 180 >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws sqs set-queue-attributes --queue-url "$(queue_url)" --region "$REGION" \
    --attributes VisibilityTimeout=180 >/dev/null
  aws lambda update-event-source-mapping --uuid "$(esm_uuid)" --region "$REGION" \
    --batch-size 10 >/dev/null
  echo "PASS: dispatch tuning restored to 180/180/10"
  ;;

*)
  echo "usage: apply-dispatch-tuning.sh <apply|verify|rollback> <region>" >&2
  exit 2
  ;;
esac
