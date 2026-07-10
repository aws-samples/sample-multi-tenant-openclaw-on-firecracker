#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# harden-config.sh — POSIX sh 幂等 openclaw.json 收敛函数(#41 的抽取)。
#
# 问题:launch-vm.sh 老版本把 controlUi.allowedOrigins / dangerouslyDisableDeviceAuth /
# chatCompletions / LiteLLM baseUrl / apiKey 收敛写进 `NEW_DATA=true` 分支——
# 一次性生成路径。唤醒(stop→start,数据盘已存在)完全跳过,配置漂移拿不到收敛:
#   • CloudFront origin 变了 → 老的 allowedOrigins 值一直用
#   • LiteLLM host 换了(堡垒机重建)→ baseUrl 指向老 IP → 401 静默
#   • chatCompletions per-tenant flag 变了 → 唤醒不生效
#   • dangerouslyDisableDeviceAuth 万一被谁塞回去也不清除
#
# 解:把幂等收敛段抽成本函数,launch-vm 每次启动(fresh + wake)都在挂盘窗口调它。
# 一次性生成的东西(gateway token / channel_secret / Cognito 注入 / config
# template 下载 / vkey 首铸)仍留在 NEW_DATA-only,不在这里。
#
# POSIX sh 限制:不用 [[、数组、<<<;所有函数在 dash/busybox/bash 下皆可跑。
# 测试(tests/test_harden_config.py)source 这份文件、直接调函数、拿真 jq 验证。

# oc_harden_config <oc_json> <cf_origin_or_empty> <litellm_baseurl_or_empty>
#                  <litellm_vkey_or_empty> <chat_ep_enabled: 1|0|"">
#
# 每次启动都跑的幂等收敛(挂盘窗口内):
#   • 无条件 del(.gateway.controlUi.dangerouslyDisableDeviceAuth)——secure default,
#     万一被谁塞回去也清掉,不假设"NEW_DATA 时清过就够"
#   • allowedOrigins:origin 非空才写(不误清空数据盘上正确的 origins)
#   • chatCompletions 三态:
#       "1"/"true"/"yes"/"on"    → enabled = true(per-tenant 开)
#       "0"/"false"/"no"/"off"   → del(chatCompletions)(secure default)
#       "" 或未知              → no-op(fail-safe:legacy 4-arg SSM 命令、
#                                   或未知值 都不许 clobber 数据盘上的现有配置。
#                                   删掉一个开着的租户的 chatCompletions =
#                                   聊天静默挂,比不动更糟。)
#   • baseUrl:非空才写(部署相关值——LiteLLM 堡垒机重建 IP 会变,
#     每次唤醒都要收敛到 platform.env 的当前值)
#   • apiKey:仅在参数显式非空才写(唤醒路径 LITELLM_VKEY 参数为空时绝不拿
#     shared key 兜底覆盖数据盘上的 per-tenant vkey——那会坏计费拆分)
#
# fail-loud:jq exit 非零、或输出空,一律不 clobber 原 openclaw.json,return 1。
# 静默吞过一次异常就是事故(踩过的教训)。
oc_harden_config() {
  __hc_oc="$1"
  __hc_origin="$2"
  __hc_baseurl="$3"
  __hc_vkey="$4"
  __hc_chat="$5"

  # 文件不存在或没 jq:跳过。openclaw.json 缺席时启动路径本就已经跑不到这里
  # (调用点先 [ -f OC_JSON ] 判过),这里保护性 return 只是给测试用。
  [ -f "${__hc_oc}" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0

  # 分段拼 jq 程序——每段独立、幂等、只碰自己的键,不误动无关字段。
  # 起手 `.` 是 identity(拿到原 JSON 不变),后面每个 `|` 是纯变换。
  __hc_prog='.'
  __hc_prog="${__hc_prog} | del(.gateway.controlUi.dangerouslyDisableDeviceAuth)"
  # 11-ENGINE-TRANSFORM(SPEC/11-ENGINE-TRANSFORM/02-DEV-PLAN.md §A):
  # 数据面转两级路由后 controlUi 必须关。无条件设 false,防有人在数据盘塞回 true,
  # 与 dangerouslyDisableDeviceAuth 同段(每次唤醒都收敛,不假设 NEW_DATA 时清过就够)。
  __hc_prog="${__hc_prog} | .gateway.controlUi.enabled = false"

  if [ -n "${__hc_origin}" ]; then
    __hc_prog="${__hc_prog} | .gateway.controlUi.allowedOrigins = [\$origin]"
  fi

  case "${__hc_chat}" in
    1|true|TRUE|yes|on)
      __hc_prog="${__hc_prog} | .gateway.http.endpoints.chatCompletions.enabled = true" ;;
    0|false|FALSE|no|off)
      __hc_prog="${__hc_prog} | del(.gateway.http.endpoints.chatCompletions)" ;;
    "")
      : ;;  # legacy 4-arg SSM:不动
    *)
      : ;;  # 未知值:不动(fail-safe)
  esac

  if [ -n "${__hc_baseurl}" ]; then
    __hc_prog="${__hc_prog} | .models.providers.litellm.baseUrl = \$baseurl"
  fi

  if [ -n "${__hc_vkey}" ]; then
    __hc_prog="${__hc_prog} | .models.providers.litellm.apiKey = \$vkey"
  fi

  __hc_tmp="${__hc_oc}.harden.$$"
  # jq 失败 / 输出空:不 clobber,报错返 1。绝不静默把好文件覆盖成空。
  if ! jq --arg origin "${__hc_origin}" \
          --arg baseurl "${__hc_baseurl}" \
          --arg vkey "${__hc_vkey}" \
          "${__hc_prog}" "${__hc_oc}" > "${__hc_tmp}" 2>/dev/null; then
    echo "[oc:harden] jq failed on ${__hc_oc} — leaving original untouched" >&2
    rm -f "${__hc_tmp}"
    return 1
  fi
  if [ ! -s "${__hc_tmp}" ]; then
    echo "[oc:harden] jq produced empty output on ${__hc_oc} (malformed input?) — leaving original untouched" >&2
    rm -f "${__hc_tmp}"
    return 1
  fi
  mv "${__hc_tmp}" "${__hc_oc}"
  return 0
}

# oc_normalize_litellm_baseurl <LITELLM_HOST_env_value>
# 打印规范化 URL 到 stdout。空输入 → 空输出。
#   已含 scheme(http://IP:4000/v1)→ 原样返回
#   纯 host/IP → 拼 http://host:4000/v1
# 防双拼(http://http://IP:4000/v1)——SSM /openclaw/litellm-host 存全 URL,老 setup 存纯 host,
# 两种都要能吃(踩过双拼致 openclaw 调 LiteLLM 必失败,CHANGELOG 有案)。
oc_normalize_litellm_baseurl() {
  __hc_h="$1"
  [ -z "${__hc_h}" ] && return 0
  case "${__hc_h}" in
    http://*|https://*) printf '%s' "${__hc_h}" ;;
    *) printf 'http://%s:4000/v1' "${__hc_h}" ;;
  esac
}

# ── Task 8.3: Frozen_Injection_Plan config-class 注入 ─────────────────────────
# oc_inject_config_from_plan <oc_json> <plan_json> <scheme> <owner_id> <region>
#                            <litellm_shared_vkey> [rsa_key_id]
#   处理 param_class=config 的条目:按 injection_target(dot-path)幂等覆盖 openclaw.json
#   empty_fallback: 值为空/缺失时使用 fallback
#   唤醒路径(plan_json 为空): 不覆盖(保留数据盘既有值)
#   scheme:kms-cmk(对称 CMK + owner_id EC)| asymmetric-v1(RSA-4096 OAEP,无 EC,方案B)
#   fail-closed: 完成后检查无 __INJECT_AT_DEPLOY__ / __LITELLM_HOST__ 残留
oc_inject_config_from_plan() {
  __ic_oc="$1"
  __ic_plan="$2"
  __ic_scheme="$3"
  __ic_owner="$4"
  __ic_region="$5"
  __ic_shared_vkey="$6"
  __ic_rsa_key_id="${7:-}"   # #149 asymmetric-v1: RSA CMK ARN (from CLAWPOOL_RSA_CMK_ARN)

  [ -z "${__ic_plan}" ] && return 0  # 无 plan(唤醒/旧契约) = 不动
  [ -f "${__ic_oc}" ] || return 0
  command -v jq >/dev/null 2>&1 || return 3

  # 提取 config-class,逐行输出 compact-JSON(一条目一行)。不用 @tsv:tab 是
  # 空白符,IFS=tab 的 read 会折叠连续 tab,value_ref 为空(llm_key 留空走
  # shared-vkey 的主用例)时中间空字段被吞、字段整体错位。改在循环内用 jq 逐字段
  # 取值,既保空字段又不受 plaintext 值含分隔符影响(单一解码路径)。
  __ic_rows="$(printf '%s' "${__ic_plan}" \
    | jq -c 'to_entries[] | select(.value.param_class == "config") | {t: .value.injection_target, m: .value.mode, v: (.value.value_ref // ""), f: (.value.empty_fallback // "")}' 2>/dev/null || true)"
  [ -z "${__ic_rows}" ] && return 0

  while IFS= read -r __ic_row; do
    [ -z "${__ic_row}" ] && continue
    __ic_target="$(printf '%s' "${__ic_row}" | jq -r '.t')"
    __ic_mode="$(printf '%s' "${__ic_row}" | jq -r '.m')"
    __ic_val="$(printf '%s' "${__ic_row}" | jq -r '.v')"
    __ic_fallback="$(printf '%s' "${__ic_row}" | jq -r '.f')"
    [ -z "${__ic_target}" ] && continue

    # 解密/取值
    if [ "${__ic_mode}" = "encrypted" ] && [ -n "${__ic_val}" ]; then
      if [ "${__ic_scheme}" = "kms-cmk" ]; then
        __ic_plain="$(printf '%s' "${__ic_val}" | base64 -d 2>/dev/null \
          | aws kms decrypt \
              --ciphertext-blob fileb:///dev/stdin \
              --encryption-context "owner_id=${__ic_owner}" \
              --region "${__ic_region}" \
              --query Plaintext --output text 2>/dev/null \
          | base64 -d 2>/dev/null || true)"
        if [ -z "${__ic_plain}" ]; then
          echo "[oc:harden] FATAL: decrypt failed for config ${__ic_target}" >&2
          return 1
        fi
      elif [ "${__ic_scheme}" = "asymmetric-v1" ]; then
        # #149 方案B — RSA-4096 OAEP-SHA256 via KMS asymmetric CMK(与 cred-inject 同源)。
        # 无 EncryptionContext(KMS 非对称 Decrypt 不支持,verified ValidationException);
        # 租户绑定 = frozen plan(field↔target)+ 信封 key_id。value_ref 是完整 enc:v1:
        # 信封,取末段(base64 密文体)KMS-decrypt。
        if [ -z "${__ic_rsa_key_id}" ]; then
          echo "[oc:harden] FATAL: asymmetric-v1 config needs CLAWPOOL_RSA_CMK_ARN (empty)" >&2
          return 1
        fi
        __ic_body="${__ic_val##*:}"   # enc:v1:...:<b64> → 最后一段 = 密文 base64
        __ic_plain="$(printf '%s' "${__ic_body}" | base64 -d 2>/dev/null \
          | aws kms decrypt \
              --ciphertext-blob fileb:///dev/stdin \
              --key-id "${__ic_rsa_key_id}" \
              --encryption-algorithm RSAES_OAEP_SHA_256 \
              --region "${__ic_region}" \
              --query Plaintext --output text 2>/dev/null \
          | base64 -d 2>/dev/null || true)"
        if [ -z "${__ic_plain}" ]; then
          echo "[oc:harden] FATAL: asymmetric-v1 decrypt failed for config ${__ic_target}" >&2
          return 1
        fi
      else
        echo "[oc:harden] FATAL: config decrypt: unknown scheme '${__ic_scheme}'" >&2
        return 1
      fi
      # 控制字符拒绝(同 cred-inject:防 \r 破坏 jq --arg 写入)
      if [ "$(printf '%s' "${__ic_plain}" | LC_ALL=C tr -dc '[:cntrl:]' | wc -c | tr -d ' ')" != "0" ]; then
        echo "[oc:harden] FATAL: config ${__ic_target} value contains control chars (rejected)" >&2
        return 1
      fi
    elif [ "${__ic_mode}" = "plaintext" ] && [ -n "${__ic_val}" ]; then
      __ic_plain="${__ic_val}"
    else
      # 空值: 用 empty_fallback
      if [ -n "${__ic_fallback}" ]; then
        # LITELLM_SHARED_VKEY 是特殊 fallback 名,值从参数传入
        case "${__ic_fallback}" in
          LITELLM_SHARED_VKEY) __ic_plain="${__ic_shared_vkey}" ;;
          *) __ic_plain="${__ic_fallback}" ;;
        esac
      else
        continue  # 非 required 且无 fallback,跳过不覆盖
      fi
    fi

    # jq 幂等写入 dot-path
    __ic_tmp="${__ic_oc}.inject.$$"
    if ! jq --arg val "${__ic_plain}" ".${__ic_target} = \$val" "${__ic_oc}" > "${__ic_tmp}" 2>/dev/null; then
      echo "[oc:harden] FATAL: jq failed setting .${__ic_target}" >&2
      rm -f "${__ic_tmp}"
      return 1
    fi
    if [ ! -s "${__ic_tmp}" ]; then
      rm -f "${__ic_tmp}"
      return 1
    fi
    mv "${__ic_tmp}" "${__ic_oc}"
  done <<IC_EOF
${__ic_rows}
IC_EOF

  # fail-closed: 残留占位符检查
  if grep -q '__INJECT_AT_DEPLOY__\|__LITELLM_HOST__' "${__ic_oc}" 2>/dev/null; then
    echo "[oc:harden] FATAL: placeholder residual in ${__ic_oc} after injection — aborting" >&2
    return 1
  fi
  return 0
}
