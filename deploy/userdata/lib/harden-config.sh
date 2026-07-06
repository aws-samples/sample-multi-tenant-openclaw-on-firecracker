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
# 静默吞过一次异常就是事故(踩过——见 CLAUDE.md 血泪教训)。
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
