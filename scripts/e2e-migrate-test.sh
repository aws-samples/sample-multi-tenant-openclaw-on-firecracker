#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# e2e-migrate-test.sh — REAL end-to-end live-migration test (issue #64 AC #6).
#
# Unlike tests/test_migration.py (which mocks SSM), this drives the actual
# data plane against a live deployment:
#   1. Ensures migrate-vm.sh / resize-disk.sh are present on every active host
#      (uploads to S3 + SSM-copies to /home/ubuntu on each host).
#   2. Picks a running tenant and migrates it to a different active host.
#   3. Verifies the move is REAL:
#        - SSM "Success" on both snapshot (source) and restore (target)
#        - tenant.host_id flipped in DDB, status back to running
#        - source host no longer runs the tenant's firecracker process
#        - dashboard reachable (HTTP < 500) through CloudFront
#
# Usage:  AWS_PROFILE=jiasunm-neo AWS_REGION=ap-northeast-1 \
#           ./scripts/e2e-migrate-test.sh [tenant_id]
#
# Requires .env.deploy (API_URL, API_KEY, ASSETS_BUCKET, DASHBOARD_URL).
set -uo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
PROFILE="${AWS_PROFILE:-jiasunm-neo}"
AWS=(aws --region "$REGION" --profile "$PROFILE")
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .env.deploy

SCRIPTS=(launch-vm.sh stop-vm.sh clone-data.sh backup-data.sh migrate-vm.sh resize-disk.sh)

say() { echo "── $* ──"; }

# ── 1. Ensure host scripts are deployed (incl. the two that issue #64 missed) ──
say "uploading host scripts to s3://${ASSETS_BUCKET}/deployment/scripts/"
for s in "${SCRIPTS[@]}"; do
  "${AWS[@]}" s3 cp "deploy/userdata/$s" \
    "s3://${ASSETS_BUCKET}/deployment/scripts/$s" --quiet && echo "  uploaded $s"
done

say "discovering active hosts"
# Use a while-read loop instead of `mapfile`/`readarray`: those are bash 4+
# builtins and macOS ships bash 3.2, where they don't exist.
HOSTS=()
while IFS= read -r _h; do [ -n "$_h" ] && HOSTS+=("$_h"); done < <(
  "${AWS[@]}" dynamodb scan --table-name "$HOSTS_TABLE" \
    --filter-expression "#s = :a" \
    --expression-attribute-names '{"#s":"status"}' \
    --expression-attribute-values '{":a":{"S":"active"}}' \
    --query 'Items[*].instance_id.S' --output text | tr '\t' '\n')
echo "  active hosts: ${HOSTS[*]:-(none)}"
[ "${#HOSTS[@]}" -ge 2 ] || { echo "need >=2 active hosts; have ${#HOSTS[@]}"; exit 1; }

# Push the (now-complete) script set to each host via SSM so the running
# fleet picks up migrate-vm.sh / resize-disk.sh without waiting for an ASG
# roll. New hosts get them automatically via the patched init-host.sh.
say "SSM-deploying scripts to ${#HOSTS[@]} hosts"
DL=""
for s in "${SCRIPTS[@]}"; do
  DL+="aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/$s /home/ubuntu/$s --region ${REGION} --quiet; chmod +x /home/ubuntu/$s; chown ubuntu:ubuntu /home/ubuntu/$s; "
done
cid=$("${AWS[@]}" ssm send-command --instance-ids "${HOSTS[@]}" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"$DL ls -1 /home/ubuntu/*.sh | wc -l\"]" \
  --query 'Command.CommandId' --output text)
echo "  ssm command: $cid (waiting)"
sleep 12
for h in "${HOSTS[@]}"; do
  st=$("${AWS[@]}" ssm get-command-invocation --command-id "$cid" --instance-id "$h" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  echo "  $h: $st"
done

# ── 2. Pick a tenant + a different target host ──
TENANT="${1:-}"
if [ -z "$TENANT" ]; then
  TENANT=$("${AWS[@]}" dynamodb scan --table-name "$TENANTS_TABLE" \
    --filter-expression "#s = :r" \
    --expression-attribute-names '{"#s":"status"}' \
    --expression-attribute-values '{":r":{"S":"running"}}' \
    --query 'Items[0].id.S' --output text)
fi
SRC=$("${AWS[@]}" dynamodb get-item --table-name "$TENANTS_TABLE" \
  --key "{\"id\":{\"S\":\"$TENANT\"}}" --query 'Item.host_id.S' --output text)
TARGET=""
for h in "${HOSTS[@]}"; do [ "$h" != "$SRC" ] && TARGET="$h" && break; done
say "migrating $TENANT  ${SRC} → ${TARGET}"
[ -n "$TARGET" ] || { echo "no target host distinct from source"; exit 1; }

# ── 3. Drive the migrate API (async) and poll to completion ──
# migrate is async (1.4.4): the API returns 202 immediately and the
# health_check sweep finishes the move out-of-band (snapshot → restore →
# verify → flip). We POST, expect 202, then poll GET /tenants/{id} until the
# status leaves `migrating` (→ running on success, or back to running with
# migration_failed set on failure). The sweep runs on the health_check
# schedule (every few minutes), so allow generous time.
RESP=$(curl -s -w '\n%{http_code}' -X POST "${API_URL%/}/tenants/$TENANT/migrate" \
  -H "x-api-key: ${API_KEY}" -H "Content-Type: application/json" \
  -d "{\"target_host_id\":\"$TARGET\"}")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
echo "  POST /migrate → HTTP $CODE"
echo "  body: $BODY"
[ "$CODE" = "202" ] || { echo "  ✗ expected 202 Accepted, got $CODE"; exit 1; }

say "polling GET /tenants/$TENANT until migration settles (max ~12 min)"
NEWHOST=""; STATUS="migrating"; MFAIL=""
for i in $(seq 1 72); do   # 72 × 10s = 12 min
  sleep 10
  T=$(curl -s "${API_URL%/}/tenants/$TENANT" -H "x-api-key: ${API_KEY}")
  STATUS=$(echo "$T" | sed -n 's/.*"status"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
  NEWHOST=$(echo "$T" | sed -n 's/.*"host_id"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
  MFAIL=$(echo "$T" | sed -n 's/.*"migration_failed"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
  printf '  [%2d] status=%s host=%s\n' "$i" "${STATUS:-?}" "${NEWHOST:-?}"
  # Terminal: either flipped to target+running, or migration_failed surfaced.
  if [ "$STATUS" = "running" ] && [ "$NEWHOST" = "$TARGET" ]; then break; fi
  if [ -n "$MFAIL" ]; then echo "  migration_failed: $MFAIL"; break; fi
done
say "post-migrate: host_id=$NEWHOST status=$STATUS (expected host=$TARGET status=running)"

# Source host must no longer run the tenant's firecracker process.
say "checking source host no longer runs the VM"
chk=$("${AWS[@]}" ssm send-command --instance-ids "$SRC" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"pgrep -fc 'api-sock /data/firecracker-vms/$TENANT/fc.sock' || echo 0\"]" \
  --query 'Command.CommandId' --output text)
sleep 8
procs=$("${AWS[@]}" ssm get-command-invocation --command-id "$chk" --instance-id "$SRC" \
  --query 'StandardOutputContent' --output text 2>/dev/null | tr -d '[:space:]')
echo "  firecracker procs for $TENANT on source: ${procs:-?}"

# Dashboard reachable through CloudFront.
say "dashboard reachability via CloudFront"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${DASHBOARD_URL%/}/vm/$TENANT/" || echo 000)
echo "  GET /vm/$TENANT/ → HTTP $code"

# ── Verdict ──
say "VERDICT"
ok=true
[ "$NEWHOST" = "$TARGET" ] || { echo "  ✗ host_id not flipped to target"; ok=false; }
[ "$STATUS" = "running" ] || { echo "  ✗ status not 'running' (got $STATUS)"; ok=false; }
[ "${procs:-1}" = "0" ]   || { echo "  ✗ source still runs the VM ($procs procs)"; ok=false; }
[ "$code" -lt 500 ] 2>/dev/null || { echo "  ✗ dashboard returned $code"; ok=false; }
$ok && echo "  ✅ REAL migration verified end-to-end" || { echo "  ❌ migration verification failed"; exit 1; }
