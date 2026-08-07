#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apply-image-jobs-table.sh — idempotently bring the openclaw-image-jobs DynamoDB table to the
# shape deploy/stacks/storage.py declares at patch a63d7b05, WITHOUT a stack update:
#   * table openclaw-image-jobs, HASH job_id, PAY_PER_REQUEST, TTL on expires_at
#   * GSI gsi_idempotency  (HASH instance_id, RANGE idempotency_key)
#   * GSI gsi_host_created (HASH instance_id, RANGE created_at)
# DynamoDB builds ONE GSI per update-table, so each Create waits ACTIVE before the next.
# Adopt-or-create: a pre-existing table/GSI is left as-is (RETAIN); only a newly-created table
# is a delete-on-rollback. Fail-closed: any AWS error aborts.
#
# Usage:  apply-image-jobs-table.sh <apply|verify|rollback> <region>
set -euo pipefail
CMD="${1:?usage: apply-image-jobs-table.sh <apply|verify|rollback> <region>}"
REGION="${2:?region required}"
TABLE="openclaw-image-jobs"

ddb() { aws dynamodb "$@" --table-name "$TABLE" --region "$REGION" --output json; }

table_status() {
  aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" \
    --query 'Table.TableStatus' --output text 2>/dev/null || echo "ABSENT"
}

gsi_status() {
  aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='$1'].IndexStatus | [0]" \
    --output text 2>/dev/null || echo "None"
}

wait_table_active() {
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
}

wait_gsi_active() {
  # poll until the named GSI reports ACTIVE (create-GSI is async; no native waiter)
  local idx="$1" i
  for i in $(seq 1 60); do
    [ "$(gsi_status "$idx")" = "ACTIVE" ] && return 0
    sleep 10
  done
  echo "FATAL: GSI $idx did not reach ACTIVE within the wait window" >&2
  return 1
}

case "$CMD" in
apply)
  created_table=false
  if [ "$(table_status)" = "ABSENT" ]; then
    echo "[image-jobs] creating table $TABLE (HASH job_id, PAY_PER_REQUEST)"
    ddb create-table \
      --attribute-definitions AttributeName=job_id,AttributeType=S \
      --key-schema AttributeName=job_id,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST >/dev/null
    wait_table_active
    created_table=true
    # record the rollback anchor: ONLY a table we created is deletable on rollback.
    echo "created_table=true" > "./.image-jobs-apply-state"
  else
    echo "[image-jobs] table $TABLE exists — adopt (RETAIN on rollback)"
    echo "created_table=false" > "./.image-jobs-apply-state"
  fi

  # TTL on expires_at (idempotent: enabling an already-enabled TTL is a no-op error we tolerate)
  ttl_state=$(aws dynamodb describe-time-to-live --table-name "$TABLE" --region "$REGION" \
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null || echo "DISABLED")
  if [ "$ttl_state" != "ENABLED" ]; then
    echo "[image-jobs] enabling TTL on expires_at"
    ddb update-time-to-live \
      --time-to-live-specification "Enabled=true,AttributeName=expires_at" >/dev/null
  fi

  # gsi_idempotency (HASH instance_id, RANGE idempotency_key) — only if absent
  if [ "$(gsi_status gsi_idempotency)" = "None" ]; then
    echo "[image-jobs] creating GSI gsi_idempotency"
    ddb update-table \
      --attribute-definitions AttributeName=instance_id,AttributeType=S AttributeName=idempotency_key,AttributeType=S \
      --global-secondary-index-updates '[{"Create":{"IndexName":"gsi_idempotency","KeySchema":[{"AttributeName":"instance_id","KeyType":"HASH"},{"AttributeName":"idempotency_key","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}}]' >/dev/null
    wait_gsi_active gsi_idempotency
  fi

  # gsi_host_created (HASH instance_id, RANGE created_at) — only if absent
  if [ "$(gsi_status gsi_host_created)" = "None" ]; then
    echo "[image-jobs] creating GSI gsi_host_created"
    ddb update-table \
      --attribute-definitions AttributeName=instance_id,AttributeType=S AttributeName=created_at,AttributeType=S \
      --global-secondary-index-updates '[{"Create":{"IndexName":"gsi_host_created","KeySchema":[{"AttributeName":"instance_id","KeyType":"HASH"},{"AttributeName":"created_at","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}}]' >/dev/null
    wait_gsi_active gsi_host_created
  fi
  echo "PASS: $TABLE ready (table + gsi_idempotency + gsi_host_created + TTL expires_at)"
  ;;

verify)
  [ "$(table_status)" = "ACTIVE" ] || { echo "FAIL: $TABLE not ACTIVE" >&2; exit 1; }
  ttl=$(aws dynamodb describe-time-to-live --table-name "$TABLE" --region "$REGION" \
    --query 'TimeToLiveDescription.AttributeName' --output text 2>/dev/null || echo "")
  [ "$ttl" = "expires_at" ] || { echo "FAIL: TTL attr is '$ttl', expected expires_at" >&2; exit 1; }
  for idx in gsi_idempotency gsi_host_created; do
    [ "$(gsi_status "$idx")" = "ACTIVE" ] || { echo "FAIL: GSI $idx not ACTIVE" >&2; exit 1; }
  done
  echo "PASS: $TABLE ACTIVE, TTL=expires_at, gsi_idempotency ACTIVE, gsi_host_created ACTIVE"
  ;;

rollback)
  # RESTORE only a table WE created this run; an adopted pre-existing table is RETAIN.
  if [ -f "./.image-jobs-apply-state" ] && grep -q "created_table=true" "./.image-jobs-apply-state"; then
    echo "[image-jobs] rollback: deleting the table this run created"
    aws dynamodb delete-table --table-name "$TABLE" --region "$REGION" --output json >/dev/null
    echo "PASS: deleted $TABLE (created by this run)"
  else
    echo "RETAIN: $TABLE pre-existed (or no apply state) — not deleting"
  fi
  ;;

*)
  echo "usage: apply-image-jobs-table.sh <apply|verify|rollback> <region>" >&2
  exit 2
  ;;
esac
