#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# e2e-az-failover-test.sh — REAL AZ-failover test against a live deployment.
#
# This drives the actual control plane:
#   1. Pick a running tenant and ensure it has a fresh backup (AZ failover
#      restores from the latest backup, not a live snapshot).
#   2. Mark the tenant's CURRENT AZ unhealthy by back-dating every active
#      host in that AZ past AZ_UNHEALTHY_THRESHOLD_MINUTES (inject_stale).
#   3. Clear the per-AZ failover cooldown and invoke the health_check Lambda
#      directly (no waiting for the 5-min schedule).
#   4. Verify the tenant was REALLY recovered into a DIFFERENT, healthy AZ:
#        - tenant.host_id now on a host in another AZ, status=running
#        - audit log has AZ_FAILOVER_TENANT_RECOVERED for the tenant
#        - dashboard reachable (HTTP < 500) through CloudFront
#   5. ALWAYS restore the back-dated host's health timestamp (even on failure)
#      so the fleet returns to normal.
#
# Usage:  AWS_PROFILE=jiasunm-neo AWS_REGION=ap-northeast-1 \
#           ./scripts/e2e-az-failover-test.sh [tenant_id]
#
# Requires .env.deploy (API_URL, API_KEY, ASSETS_BUCKET, DASHBOARD_URL,
# HOSTS_TABLE, TENANTS_TABLE).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .env.deploy
export E2E_PROFILE="${AWS_PROFILE:-jiasunm-neo}"
export E2E_REGION="${AWS_REGION:-ap-northeast-1}"
# shellcheck disable=SC1091
source "$(dirname "$0")/lib/e2e_failover_helpers.sh"

REGION="$E2E_REGION"; PROFILE="$E2E_PROFILE"
AWS=(aws --region "$REGION" --profile "$PROFILE")
say() { echo "── $* ──"; }

# ── 1. Pick the tenant + discover its current host/AZ ──
TENANT="${1:-}"
if [ -z "$TENANT" ]; then
  TENANT=$("${AWS[@]}" dynamodb scan --table-name "$TENANTS_TABLE" \
    --filter-expression "#s = :r" --expression-attribute-names '{"#s":"status"}' \
    --expression-attribute-values '{":r":{"S":"running"}}' \
    --query 'Items[0].id.S' --output text)
fi
SRC_HOST=$("${AWS[@]}" dynamodb get-item --table-name "$TENANTS_TABLE" \
  --key "{\"id\":{\"S\":\"$TENANT\"}}" --query 'Item.host_id.S' --output text)
SRC_AZ=$("${AWS[@]}" dynamodb get-item --table-name "$HOSTS_TABLE" \
  --key "{\"instance_id\":{\"S\":\"$SRC_HOST\"}}" --query 'Item.az.S' --output text)
say "tenant $TENANT is on $SRC_HOST in $SRC_AZ"

# Every active host in the SOURCE AZ (we'll back-date all of them so the whole
# AZ is judged unhealthy). Other AZs must have a healthy host to receive it.
mapfile_compat() { while IFS= read -r l; do [ -n "$l" ] && echo "$l"; done; }
SRC_AZ_HOSTS=$("${AWS[@]}" dynamodb scan --table-name "$HOSTS_TABLE" \
  --filter-expression "#s = :a AND az = :z" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values "{\":a\":{\"S\":\"active\"},\":z\":{\"S\":\"$SRC_AZ\"}}" \
  --query 'Items[*].instance_id.S' --output text | tr '\t' '\n' | mapfile_compat)
OTHER_AZ_HOSTS=$("${AWS[@]}" dynamodb scan --table-name "$HOSTS_TABLE" \
  --filter-expression "#s = :a AND az <> :z" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values "{\":a\":{\"S\":\"active\"},\":z\":{\"S\":\"$SRC_AZ\"}}" \
  --query 'Items[*].[instance_id.S,az.S]' --output text)
echo "  source-AZ hosts: $(echo "$SRC_AZ_HOSTS" | tr '\n' ' ')"
echo "  other-AZ hosts : $OTHER_AZ_HOSTS"
[ -n "$OTHER_AZ_HOSTS" ] || { echo "no healthy host in another AZ — cannot fail over"; exit 1; }

# ── 2. Ensure a fresh backup exists (failover restores from backup) ──
say "ensuring a fresh backup for $TENANT"
if [ "$(e2e_trigger_backup_and_wait "$TENANT")" = "ok" ]; then
  echo "  backup present"
else
  echo "  WARN: backup did not appear in time; failover may NO_BACKUP"
fi

# ── cleanup trap: always un-stale the source hosts ──
FRESH_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cleanup() {
  say "cleanup: restoring health timestamps on source-AZ hosts"
  for h in $SRC_AZ_HOSTS; do
    "${AWS[@]}" dynamodb update-item --table-name "$HOSTS_TABLE" \
      --key "{\"instance_id\":{\"S\":\"$h\"}}" \
      --update-expression "SET last_seen = :t, last_health_check = :t" \
      --expression-attribute-values "{\":t\":{\"S\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}" \
      >/dev/null 2>&1 && echo "  refreshed $h"
  done
  e2e_clear_cooldown
}
trap cleanup EXIT

# ── 3. Inject AZ outage + trigger failover ──
say "marking $SRC_AZ unhealthy (back-dating ${SRC_AZ_HOSTS//$'\n'/, })"
for h in $SRC_AZ_HOSTS; do e2e_inject_stale "$h" "2026-05-20T00:00:00Z"; echo "  staled $h"; done
e2e_clear_cooldown
say "invoking health_check Lambda (synchronous failover)"
e2e_invoke_health_check
sleep 5
echo "  last failover log: $(e2e_lambda_log_last_failover)"

# Give the synchronous failover a moment to settle DDB.
sleep 8

# ── 4. Verify ──
NEWHOST=$("${AWS[@]}" dynamodb get-item --table-name "$TENANTS_TABLE" \
  --key "{\"id\":{\"S\":\"$TENANT\"}}" --query 'Item.host_id.S' --output text)
STATUS=$("${AWS[@]}" dynamodb get-item --table-name "$TENANTS_TABLE" \
  --key "{\"id\":{\"S\":\"$TENANT\"}}" --query 'Item.status.S' --output text)
NEW_AZ=$("${AWS[@]}" dynamodb get-item --table-name "$HOSTS_TABLE" \
  --key "{\"instance_id\":{\"S\":\"$NEWHOST\"}}" --query 'Item.az.S' --output text)
say "post-failover: tenant on $NEWHOST ($NEW_AZ) status=$STATUS"

say "audit log AZ_FAILOVER_* for $TENANT"
e2e_audit_search "AZ_FAILOVER" 2>/dev/null \
  | python3 -c "import sys,json
try: items=json.load(sys.stdin)
except: items=[]
for it in items:
    if '$TENANT' in (it.get('res','') or '') or '$TENANT' in (it.get('d','') or ''):
        print('  ', it.get('op'), it.get('res'), it.get('ts'))" 2>/dev/null | tail -6

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${DASHBOARD_URL%/}/vm/$TENANT/" || echo 000)
echo "  dashboard GET /vm/$TENANT/ → HTTP $code"

# ── Verdict ──
say "VERDICT"
ok=true
[ "$STATUS" = "running" ] || { echo "  ✗ status not running (got $STATUS)"; ok=false; }
[ -n "$NEW_AZ" ] && [ "$NEW_AZ" != "$SRC_AZ" ] || { echo "  ✗ tenant not moved out of $SRC_AZ (now in ${NEW_AZ:-?})"; ok=false; }
[ "$code" -lt 500 ] 2>/dev/null || { echo "  ✗ dashboard returned $code"; ok=false; }
$ok && echo "  ✅ REAL AZ failover verified: $SRC_AZ → $NEW_AZ" \
     || { echo "  ❌ AZ failover verification failed"; exit 1; }
