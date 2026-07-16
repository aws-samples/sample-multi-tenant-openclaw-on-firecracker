#!/bin/bash
# ============================================================================
# PATCH #266 — Add TENANT_SECRETS_TABLE to /etc/platform.env
# ============================================================================
# Run this ON THE HOST (metal instance) as root or ubuntu.
# Idempotent: won't duplicate if already present.
# ============================================================================
set -euo pipefail

ENV_FILE="/etc/platform.env"

if ! grep -q '^TENANT_SECRETS_TABLE=' "${ENV_FILE}" 2>/dev/null; then
  echo "TENANT_SECRETS_TABLE=openclaw-tenant-secrets" >> "${ENV_FILE}"
  echo "[patch-266] Added TENANT_SECRETS_TABLE to ${ENV_FILE}"
else
  echo "[patch-266] TENANT_SECRETS_TABLE already in ${ENV_FILE}, skipping"
fi
