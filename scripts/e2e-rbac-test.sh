#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# e2e-rbac-test.sh — REAL RBAC fail-safe + JWT-forgery verification against a
# live deployment (1.5.0 security hardening).
#
# Unlike tests/test_rbac.py (which unit-tests _get_user_role with a patched
# JWKS seam), this drives the deployed openclaw-api through API Gateway and
# proves the production Lambda actually:
#   1. fails SAFE — a request with NO Bearer token resolves to `viewer`, so
#      writes (POST/DELETE) return 403 while reads (GET) return 200;
#   2. rejects FORGERY — a token signed by an attacker key (and an alg:none
#      unsigned token), both claiming cognito:groups=[admin], are rejected by
#      RS256 signature verification and downgraded to `viewer` → 403.
#
# This is the live counterpart to the pre-1.5.0 exploit: the same forged-admin
# payload that used to grant admin now yields viewer/403.
#
# Usage:  AWS_PROFILE=jiasunm-neo AWS_REGION=ap-northeast-1 \
#           ./scripts/e2e-rbac-test.sh
#
# Requires .env.deploy (API_URL, API_KEY) and the `test` deps (pyjwt,
# cryptography) for minting the forged tokens locally.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .env.deploy
API="${API_URL%/}"
POOL="${COGNITO_USER_POOL_ID:-ap-northeast-1_yvmL2GT3P}"
ISS="https://cognito-idp.${REGION:-ap-northeast-1}.amazonaws.com/${POOL}"

say() { echo "── $* ──"; }
ok=true
pass() { echo "  ✅ $*"; }
fail() { echo "  ❌ $*"; ok=false; }

# ── 1. no-token writes must 403 (fail-safe), reads must 200 (viewer) ──
say "no Bearer token → viewer (fail-safe)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/tenants" \
  -H "x-api-key: ${API_KEY}" -H "Content-Type: application/json" \
  -d '{"name":"rbac-probe-noauth"}' --max-time 20)
[ "$code" = "403" ] && pass "POST /tenants (no token) → 403" \
                     || fail "POST /tenants (no token) → $code (want 403)"

code=$(curl -s -o /dev/null -w '%{http_code}' "$API/tenants" \
  -H "x-api-key: ${API_KEY}" --max-time 20)
[ "$code" = "200" ] && pass "GET /tenants (no token) → 200 (viewer can read)" \
                     || fail "GET /tenants (no token) → $code (want 200)"

code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/tenants/nonexistent-probe" \
  -H "x-api-key: ${API_KEY}" --max-time 20)
[ "$code" = "403" ] && pass "DELETE /tenants/{id} (no token) → 403" \
                     || fail "DELETE /tenants/{id} (no token) → $code (want 403)"

# ── 2. forged tokens must be rejected by signature verification ──
say "forged admin token (attacker RS256 key) → rejected"
FORGED=$(uv run python - "$ISS" <<'PY'
import sys, jwt, time
from cryptography.hazmat.primitives.asymmetric import rsa
iss = sys.argv[1]
k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
now = int(time.time())
print(jwt.encode({"iss": iss, "cognito:groups": ["admin"], "exp": now + 3600,
                  "iat": now, "token_use": "id", "email": "attacker@evil"},
                 k, algorithm="RS256"))
PY
)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/tenants" \
  -H "x-api-key: ${API_KEY}" -H "Authorization: Bearer ${FORGED}" \
  -H "Content-Type: application/json" -d '{"name":"rbac-probe-forged"}' --max-time 20)
[ "$code" = "403" ] && pass "POST with forged-admin token → 403 (signature rejected)" \
                     || fail "POST with forged-admin token → $code (want 403 — verification may be OFF!)"

say "alg:none unsigned admin token → rejected"
NONE_TOK=$(uv run python - <<'PY'
import base64, json, time
def b(o): return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
now = int(time.time())
print(b({"alg": "none", "typ": "JWT"}) + "."
      + b({"cognito:groups": ["admin"], "exp": now + 3600, "iss": "x"}) + ".")
PY
)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/tenants" \
  -H "x-api-key: ${API_KEY}" -H "Authorization: Bearer ${NONE_TOK}" \
  -H "Content-Type: application/json" -d '{"name":"rbac-probe-none"}' --max-time 20)
[ "$code" = "403" ] && pass "POST with alg:none token → 403" \
                     || fail "POST with alg:none token → $code (want 403)"

# ── Verdict ──
say "VERDICT"
$ok && echo "  ✅ RBAC fail-safe + JWT forgery rejection verified live" \
     || { echo "  ❌ RBAC live verification FAILED"; exit 1; }
