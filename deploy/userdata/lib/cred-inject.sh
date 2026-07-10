#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# cred-inject.sh — POSIX sh helper for #118/#116 platform-injected credentials.
#
# Extracted from launch-vm.sh so the security-critical fail-closed decrypt path
# is unit-testable (tests/test_cred_inject.sh source this file, stub `aws`, and
# assert the .env output + non-zero exit on any decrypt failure). The ext4/mount
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

# ── Task 8.2: Frozen_Injection_Plan 消费(新契约) ──────────────────────────────
# oc_decrypt_frozen_plan <plan_json> <owner_id> <scheme> <region> <out_env_file>
#   plan_json = Frozen_Injection_Plan JSON(从 DDB 读出的 tenant.frozen_injection_plan)
#              {"field": {"param_class":"env","injection_target":"ENV_NAME","mode":"encrypted","value_ref":"enc:v1:..."}, ...}
#   scheme    = kms-cmk | asymmetric-v1
#   只处理 param_class=env 的条目(config-class 由 harden-config.sh 处理)
#   返回 0 + 写 dotenv 行;非零 = fail-closed(截断输出)。
oc_decrypt_frozen_plan() {
  __fp_json="$1"
  __fp_owner="$2"
  __fp_scheme="$3"
  __fp_region="$4"
  __fp_out="$5"
  __fp_rsa_key_id="${6:-}"   # #149 asymmetric-v1: RSA CMK ARN (from CLAWPOOL_RSA_CMK_ARN)

  [ -z "${__fp_json}" ] && return 0  # 无 plan = 旧契约,跳过
  command -v jq >/dev/null 2>&1 || return 3

  # 提取 env-class 条目,逐行 compact-JSON(不用 @tsv:tab 是空白符,IFS=tab 的
  # read 会折叠连续 tab / 丢末尾空字段,value_ref 为空或含分隔符时字段错位)。
  # 循环内用 jq 逐字段取值,单一解码路径,保空字段。
  __fp_rows="$(printf '%s' "${__fp_json}" \
    | jq -c 'to_entries[] | select(.value.param_class == "env") | {t: .value.injection_target, m: .value.mode, v: (.value.value_ref // "")}' 2>/dev/null || true)"
  [ -z "${__fp_rows}" ] && return 0  # 无 env-class 条目

  __fp_count=0
  while IFS= read -r __fp_row; do
    [ -z "${__fp_row}" ] && continue
    __fp_target="$(printf '%s' "${__fp_row}" | jq -r '.t')"
    __fp_mode="$(printf '%s' "${__fp_row}" | jq -r '.m')"
    __fp_val="$(printf '%s' "${__fp_row}" | jq -r '.v')"
    [ -z "${__fp_target}" ] && continue

    if [ "${__fp_mode}" = "plaintext" ]; then
      __fp_plain="${__fp_val}"
    elif [ "${__fp_mode}" = "encrypted" ]; then
      # 按 scheme 解密
      if [ "${__fp_scheme}" = "kms-cmk" ]; then
        __fp_plain="$(printf '%s' "${__fp_val}" | base64 -d 2>/dev/null \
          | aws kms decrypt \
              --ciphertext-blob fileb:///dev/stdin \
              --encryption-context "owner_id=${__fp_owner}" \
              --region "${__fp_region}" \
              --query Plaintext --output text 2>/dev/null \
          | base64 -d 2>/dev/null || true)"
      elif [ "${__fp_scheme}" = "asymmetric-v1" ]; then
        # #149 — RSA-4096 OAEP-SHA256 via KMS asymmetric CMK. Private key never
        # leaves KMS (host holds no key); host has kms:Decrypt on the RSA CMK.
        # value_ref is a full enc:v1: envelope: enc:v1:<alg>:<keyid>:<hybrid>:<b64>.
        # We take the last ':'-field (base64 ciphertext body) and KMS-decrypt it.
        # NOTE (scheme-B): KMS asymmetric Decrypt does NOT accept EncryptionContext
        # (verified: ValidationException), so there is NO KMS-level AAD binding here;
        # tenant binding is the frozen plan (field↔target) + envelope key_id.
        if [ -z "${__fp_rsa_key_id}" ]; then
          echo "[oc:cred] FATAL: asymmetric-v1 needs CLAWPOOL_RSA_CMK_ARN (empty)" >&2
          : > "${__fp_out}"; return 1
        fi
        __fp_body="${__fp_val##*:}"   # enc:v1:...:<b64> → 最后一段 = 密文 base64
        __fp_plain="$(printf '%s' "${__fp_body}" | base64 -d 2>/dev/null \
          | aws kms decrypt \
              --ciphertext-blob fileb:///dev/stdin \
              --key-id "${__fp_rsa_key_id}" \
              --encryption-algorithm RSAES_OAEP_SHA_256 \
              --region "${__fp_region}" \
              --query Plaintext --output text 2>/dev/null \
          | base64 -d 2>/dev/null || true)"
      else
        echo "[oc:cred] FATAL: unknown scheme '${__fp_scheme}'" >&2
        : > "${__fp_out}"
        return 1
      fi
      if [ -z "${__fp_plain}" ]; then
        echo "[oc:cred] FATAL: decrypt failed for ${__fp_target} (scheme=${__fp_scheme})" >&2
        : > "${__fp_out}"
        return 1
      fi
    else
      echo "[oc:cred] FATAL: unknown mode '${__fp_mode}' for ${__fp_target}" >&2
      : > "${__fp_out}"
      return 1
    fi
    # 控制字符拒绝(同 oc_decrypt_injected_creds)
    if [ "$(printf '%s' "${__fp_plain}" | LC_ALL=C tr -dc '[:cntrl:]' | wc -c | tr -d ' ')" != "0" ]; then
      echo "[oc:cred] FATAL: ${__fp_target} value contains control chars (rejected)" >&2
      : > "${__fp_out}"
      return 1
    fi
    printf '%s=%s\n' "${__fp_target}" "${__fp_plain}" >> "${__fp_out}"
    __fp_count=$((__fp_count + 1))
  done <<FP_EOF
${__fp_rows}
FP_EOF

  echo "${__fp_count}"
  return 0
}
