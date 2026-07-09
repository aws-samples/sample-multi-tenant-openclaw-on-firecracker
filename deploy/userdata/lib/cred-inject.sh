#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# cred-inject.sh — POSIX sh helper for #118/#116 platform-injected credentials.
#
# Extracted from launch-vm.sh so the security-critical fail-closed decrypt path
# is unit-testable (source this file, stub `aws`, and assert the .env output +
# non-zero exit on any decrypt failure). The ext4/mount
# steps stay in launch-vm.sh (root-only, verified on a real host).
#
# oc_decrypt_injected_creds <ic_json> <owner_id> <region> <out_env_file>
#   ic_json      = the tenant record's injected_credentials.M object (DDB JSON),
#                  i.e. {"kms_encrypted":{...},"kms_key_arn":{...},"items":{"L":[...]}}
#   owner_id     = the EncryptionContext binding (owner_id=<id>); a ciphertext
#                  minted for another user fails to decrypt (cross-user guard). We
#                  bind owner_id (platform user identity, present at userkey-create
#                  time), NOT tenant_id (which doesn't exist when the upstream
#                  registration center encrypts the key).
#   region       = AWS region for `aws kms decrypt`.
#   out_env_file = dotenv file to APPEND `NAME=value` lines to (caller pre-creates
#                  it 0600). On any failure we truncate it (no partial creds) and
#                  return non-zero — fail-closed. The caller MUST treat non-zero as
#                  fatal (never boot a VM with half/empty injected creds).
#
# Returns 0 + writes N lines on full success; non-zero on ANY failure (malformed
# record, KMS error/EC mismatch/tampered blob, multi-line value). Never returns 0
# with a silently-empty value.
oc_decrypt_injected_creds() {
  __ci_json="$1"
  __ci_owner="$2"
  __ci_region="$3"
  __ci_out="$4"

  [ -z "${__ci_json}" ] && return 2
  command -v jq >/dev/null 2>&1 || return 3

  # items is a DDB List(L) of Map(M){name:{S},ciphertext:{S}}. Emit name<TAB>ct.
  __ci_rows="$(printf '%s' "${__ci_json}" \
    | jq -r '.items.L[] | [.M.name.S, .M.ciphertext.S] | @tsv' 2>/dev/null || true)"
  if [ -z "${__ci_rows}" ]; then
    echo "[oc:cred] no items parsed from injected_credentials (malformed)" >&2
    : > "${__ci_out}"
    return 2
  fi

  __ci_count=0
  # Read rows via a here-doc so the loop runs in THIS shell (a pipe would put the
  # while in a subshell and lose __ci_count / the early return).
  while IFS="$(printf '\t')" read -r __ci_name __ci_ct; do
    [ -z "${__ci_name}" ] && continue
    __ci_plain="$(printf '%s' "${__ci_ct}" | base64 -d 2>/dev/null \
      | aws kms decrypt \
          --ciphertext-blob fileb:///dev/stdin \
          --encryption-context "owner_id=${__ci_owner}" \
          --region "${__ci_region}" \
          --query Plaintext --output text 2>/dev/null \
      | base64 -d 2>/dev/null || true)"
    if [ -z "${__ci_plain}" ]; then
      echo "[oc:cred] FATAL: KMS decrypt failed for ${__ci_name} (EC mismatch / no perm / bad ciphertext)" >&2
      : > "${__ci_out}"
      return 1
    fi
    # A dotenv line is NAME=value to end-of-line; ANY control char can smuggle a
    # second variable. `wc -l` only counts \n — but the guest's dotenv loader
    # (dotenv@17.3.1) normalizes a bare \r to a newline BEFORE parsing, so a value
    # like "AKIA\rNODE_OPTIONS=--require /evil.js" would split into two env vars
    # (RCE via NODE_OPTIONS before the in-guest guards load). So reject the whole
    # control-char class (\r \n \t NUL etc.), not just \n. Count control BYTES via
    # `tr -dc [:cntrl:] | wc -c`: byte-oriented (grep is line-oriented and would
    # never SEE an interior \n) AND count-based (a bare `-n "$(...)"` test fails on
    # a lone \n because $() strips trailing newlines — the same trap wc -l fell into).
    if [ "$(printf '%s' "${__ci_plain}" | LC_ALL=C tr -dc '[:cntrl:]' | wc -c | tr -d ' ')" != "0" ]; then
      echo "[oc:cred] FATAL: ${__ci_name} decodes to a value with control chars (rejected)" >&2
      : > "${__ci_out}"
      return 1
    fi
    printf '%s=%s\n' "${__ci_name}" "${__ci_plain}" >> "${__ci_out}"
    __ci_count=$((__ci_count + 1))
  done <<CRED_EOF
${__ci_rows}
CRED_EOF

  [ "${__ci_count}" -gt 0 ] || { : > "${__ci_out}"; return 1; }
  echo "${__ci_count}"
  return 0
}
