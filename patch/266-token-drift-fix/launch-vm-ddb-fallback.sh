#!/bin/bash
# ============================================================================
# PATCH #266 — gateway token / device paired DDB fallback
# ============================================================================
# Insert this block into launch-vm.sh AFTER the #199 fix block (line ~459)
# and BEFORE the harden-config.sh source block (line ~460).
#
# Purpose: When positional arg 12 (gateway_token_ct) or 13 (device_paired_b64)
# is empty (host-agent recovery / manual relaunch / SSM wake), read them from
# DDB openclaw-tenant-secrets instead of falling through to openssl rand.
# ============================================================================

# #266 fix: host-agent _recover_vm / _force_relaunch_vm only pass 4 positional
# args — positions 12/13 are empty. If NEW_DATA=true (fresh data disk), the
# token injection block falls back to openssl rand, producing a random token
# that doesn't match what DDB stores. JDWS gets token A from the API, VM has
# token B → connection refused. Same issue for device paired.json → no
# auto-pair → manual approve required.
# Fix: self-serve from tenant_secrets when positional args are empty.
# Fail-CLOSED: DDB read failure → exit 1 (scheduler retries).
if [ -z "${INJECTED_GATEWAY_TOKEN_CT}" ] || [ -z "${INJECTED_DEVICE_PAIRED_B64}" ]; then
  _SECRETS_TABLE="${TENANT_SECRETS_TABLE:-openclaw-tenant-secrets}"
  if _SEC_RAW="$(aws dynamodb get-item \
    --table-name "${_SECRETS_TABLE}" \
    --key "{\"tenant_id\":{\"S\":\"${TENANT_ID}\"}}" \
    --projection-expression 'gateway_token_ct, device_paired_b64' \
    --consistent-read \
    --region "${OC_REGION:-ap-northeast-1}" \
    --output json 2>/dev/null)"; then
    [ -z "${INJECTED_GATEWAY_TOKEN_CT}" ] && INJECTED_GATEWAY_TOKEN_CT="$(printf '%s' "${_SEC_RAW}" | jq -r '.Item.gateway_token_ct.S // ""' 2>/dev/null || true)"
    [ -z "${INJECTED_DEVICE_PAIRED_B64}" ] && INJECTED_DEVICE_PAIRED_B64="$(printf '%s' "${_SEC_RAW}" | jq -r '.Item.device_paired_b64.S // ""' 2>/dev/null || true)"
    [ -n "${INJECTED_GATEWAY_TOKEN_CT}" ] && log "DDB fallback: got gateway_token_ct from ${_SECRETS_TABLE} (#266)"
    [ -n "${INJECTED_DEVICE_PAIRED_B64}" ] && log "DDB fallback: got device_paired_b64 from ${_SECRETS_TABLE} (#266)"
  else
    echo "[oc:launch] FATAL(#266): DDB get-item for gateway_token_ct/device_paired_b64 failed (throttle/IAM/network) — fail-closed, scheduler will retry" >&2
    exit 1
  fi
  unset _SEC_RAW _SECRETS_TABLE
fi
