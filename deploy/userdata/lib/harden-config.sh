#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
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
#   • allowedOrigins:origin 非空才写(不误清空数据盘上正确的 origins)。注意这是【整数组
#     替换】,不是追加 —— 传进来的那一个值决定了该租户唯一能连的 Origin。三种结局都出日志:
#     收窄到具体值 / 收窄成通配符(= 校验实际关闭) / 空值跳过(= 保留盘上现值,可能也是通配符)。
#     后两种在"没报错"这件事上和第一种完全一样,所以必须靠日志区分,否则失配要等租户
#     下次启动才发作,且现场归因不到这里。
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
#   • LiteLLM provider request timeout:每次固定收敛到 55s。OpenClaw v2026.7.1
#     原生支持 models.providers.<id>.timeoutSeconds,覆盖 connect/headers/body/stream
#     watchdog；edge 的 chat-only idle timeout 是 60s,留 5s 把错误 SSE 刷给客户端。
#
# fail-loud:jq exit 非零、或输出空,一律不 clobber 原 openclaw.json,return 1。
# 静默吞过一次异常就是事故(踩过——见 CLAUDE.md 血泪教训)。
oc_harden_config() {
  __hc_oc="$1"
  __hc_origin="$2"
  __hc_baseurl="$3"
  __hc_vkey="$4"
  __hc_chat="$5"
  __hc_llm_timeout=55

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

  # allowedOrigins 是【整数组替换】而不是追加:收窄后只有 ${__hc_origin} 这一个值能连。
  # 三条分支都必须出日志。这一步失配的症状是 guest 内 gateway 回
  # "Rejected: origin not allowed / exit=INVALID_REQUEST",而且要等租户【下次启动】才发作
  # —— 现场只看得到"某个租户连不上",看不出是这里改的,更看不出这个值从哪来。
  # 日志是唯一的归因锚点:三种结局(收窄成功/收窄成通配符/根本没收窄)在配置上截然不同,
  # 而在"没报错"这件事上完全一样。
  if [ "${__hc_origin}" = "*" ]; then
    # 通配符会让收窄"成功"但收窄到没有限制。这条必须与下面的成功分支分开报:
    # 写成 narrowed 会被读成收窄生效,而实际是 Origin 校验被关掉了 —— 业务能通,
    # 但那是不设防状态,不是配置正确。
    __hc_prog="${__hc_prog} | .gateway.controlUi.allowedOrigins = [\$origin]"
    echo "[oc:harden] WARN: allowedOrigins set to wildcard ['*'] —" \
      "the Origin check is effectively DISABLED for this tenant; any origin may connect" >&2
  elif [ -n "${__hc_origin}" ]; then
    __hc_prog="${__hc_prog} | .gateway.controlUi.allowedOrigins = [\$origin]"
    echo "[oc:harden] allowedOrigins narrowed to ['${__hc_origin}']" \
      "— clients sending any other Origin will be rejected" >&2
  else
    # 刻意的 fail-safe(见上文注释):空值不写,不误清数据盘上已有的正确 origins。
    # 但沉默的代价是租户可能停留在 config 模板默认值 ["*"],即 Origin 校验实际关闭。
    echo "[oc:harden] WARN: origin arg empty — skipping allowedOrigins narrowing;" \
      "tenant keeps whatever is on disk (a template default of [\"*\"] means the" \
      "Origin check is effectively OFF)" >&2
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
  __hc_prog="${__hc_prog} | .models.providers.litellm.timeoutSeconds = \$llm_timeout"

  __hc_tmp="${__hc_oc}.harden.$$"
  # jq 失败 / 输出空:不 clobber,报错返 1。绝不静默把好文件覆盖成空。
  if ! jq --arg origin "${__hc_origin}" \
          --arg baseurl "${__hc_baseurl}" \
          --arg vkey "${__hc_vkey}" \
          --argjson llm_timeout "${__hc_llm_timeout}" \
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
#   已含 scheme(http://IP:4000/v1 或 https://gw/v1)→ 原样返回(防双拼)
#   纯 host/IP → 按 env 派生 scheme/port/path 拼(R15.1:不再死拼 http:4000)
# R15.1(N4 病根):旧版无 scheme 一律硬拼 `http://%s:4000/v1`,HTTPS 网关(如客户
# 自建 TLS LiteLLM,443/无端口)被拼成 http://gw:4000/v1 必失败、"调试很久"。改为从
# 可配 env 派生:LITELLM_SCHEME(默认 http)、LITELLM_PORT(默认 4000,空=不带端口)、
# 防双拼(http://http://IP...)语义保留:输入已含 scheme 原样返回,绝不二次拼。
oc_normalize_litellm_baseurl() {
  __hc_h="$1"
  [ -z "${__hc_h}" ] && return 0
  case "${__hc_h}" in
    http://*|https://*) printf '%s' "${__hc_h}"; return 0 ;;
  esac
  # 纯 host/IP:按 env 派生(有安全默认,保持旧行为 http:4000/v1 为默认)
  __hc_scheme="${LITELLM_SCHEME:-http}"
  __hc_port="${LITELLM_PORT-4000}"   # 用 - 不用 :- ,允许显式空(HTTPS 443 场景不带端口)
  __hc_path="${LITELLM_PATH-/v1}"
  if [ -n "${__hc_port}" ]; then
    printf '%s://%s:%s%s' "${__hc_scheme}" "${__hc_h}" "${__hc_port}" "${__hc_path}"
  else
    printf '%s://%s%s' "${__hc_scheme}" "${__hc_h}" "${__hc_path}"
  fi
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
        # 特殊 fallback 名 → 映射到 host 侧的平台默认值(不是字面量):
        #   LITELLM_SHARED_VKEY  → 平台 shared vkey(从参数传入)
        #   LITELLM_HOST_DEFAULT → 平台全局 litellm baseUrl(客户未自带网关时兜底,
        #     值 = 规范化后的 LITELLM_HOST env,来自 /etc/platform.env / SSM;
        #     与不带 llm_base_url 的现状行为一致)。LITELLM_HOST 空则跳过不覆盖,
        #     让 openclaw.json 保留其原 baseUrl(避免写空串破坏配置)。
        case "${__ic_fallback}" in
          LITELLM_SHARED_VKEY) __ic_plain="${__ic_shared_vkey}" ;;
          LITELLM_HOST_DEFAULT)
            if [ -n "${LITELLM_HOST:-}" ]; then
              __ic_plain="$(oc_normalize_litellm_baseurl "${LITELLM_HOST}")"
            else
              continue  # 无平台默认 baseUrl,不覆盖数据盘现值
            fi
            ;;
          *) __ic_plain="${__ic_fallback}" ;;
        esac
      else
        continue  # 非 required 且无 fallback,跳过不覆盖
      fi
    fi

    # jq 幂等写入 dot-path。target 只作为 --arg 数据传入,split 后得到 key array,
    # setpath 不把连字符/数字段当 jq 源码(`claw-channel` 不再被解析成减法)。
    __ic_tmp="${__ic_oc}.inject.$$"
    if ! jq --arg target "${__ic_target}" --arg val "${__ic_plain}" \
        'setpath(($target | split(".")); $val)' \
        "${__ic_oc}" > "${__ic_tmp}" 2>/dev/null; then
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

  # 残留占位符检查:WARN,不 abort launch(改自原 FATAL/return 1)。
  # 病史(新加坡真机 2026-07-13):客户只传 llm_base_url 不传 llm_key、且平台 shared vkey
  # 未配时,apiKey 占位 __INJECT_AT_DEPLOY__ 无人填 → 这里原本 FATAL return 1 → launch
  # abort → VM 起不来(且挂载窗口内退出漏 umount data.ext4,重试时 mount 撞挂载点,租户
  # 永久 recovering)。但同一个 apiKey 残留在 oc_harden_config 那边只是 WARN("apiKey 保留
  # 占位符,LLM 调用会 401")——两处对同一占位符一 FATAL 一 WARN 不一致。残留的后果是
  # 运行时 401/LLM 连不上(可观测、可后补 key),**不该让整台 VM 起不来**(更糟)。故降为
  # WARN:launch 照常起,gateway 活,只是 LLM 调用会 401 直到补上 key/baseUrl。
  if grep -q '__INJECT_AT_DEPLOY__\|__LITELLM_HOST__' "${__ic_oc}" 2>/dev/null; then
    echo "[oc:harden] WARN: placeholder residual in ${__ic_oc} after injection" \
         "(未注入的 apiKey/baseUrl 占位仍在 → 该租户 LLM 调用会 401/连不上,补 llm_key/" \
         "llm_base_url 或平台 shared vkey 后即恢复;VM 照常起,不 abort)" >&2
  fi
  return 0
}

# oc_assemble_config <current_json> <template_json> <output_json>
#                    <frozen_plan_json> <credentials_json>
#                    <scheme> <owner_id> <region>
#                    <cf_origin> <litellm_baseurl> <shared_vkey> <chat_enabled>
#                    [rsa_key_id]
#
# One implementation serves both callers:
#   · pre-rebuild probe:current={} + placeholder disk credentials;
#   · launch commit:current=mounted data-disk config + real disk credentials.
#
# The target template may only replace platform-managed top-level bodies. Unknown
# customer-owned top-level keys survive. Injection and hardening then run in the
# same order for probe and boot. Finally non-empty disk credentials are restored,
# so target body/fallbacks can never overwrite gateway.auth.token or a tenant vkey.
oc_assemble_config() {
  __ac_current="$1"
  __ac_template="$2"
  __ac_output="$3"
  __ac_plan="$4"
  __ac_creds="$5"
  __ac_scheme="$6"
  __ac_owner="$7"
  __ac_region="$8"
  __ac_origin="$9"
  shift 9
  __ac_baseurl="$1"
  __ac_shared_vkey="$2"
  __ac_chat="$3"
  __ac_rsa="${4:-}"

  [ -f "${__ac_current}" ] || return 1
  [ -f "${__ac_template}" ] || return 1
  command -v jq >/dev/null 2>&1 || return 1

  __ac_tmp="${__ac_output}.assemble.$$"
  __ac_final="${__ac_output}.final.$$"
  rm -f "${__ac_tmp}" "${__ac_final}"
  if ! jq -s '
      def managed:
        ["agents", "gateway", "models", "plugins", "tools", "mcp",
         "channels", "commands", "messages", "session", "browser",
         "hooks", "skills"];
      .[0] as $current
      | .[1] as $template
      | if ($current | type) != "object" or ($template | type) != "object"
        then error("openclaw config/template must be objects")
        else reduce managed[] as $key
          ($current; if ($template | has($key))
                     then setpath(
                       [$key];
                       $template[$key])
                     else . end)
        end
    ' "${__ac_current}" "${__ac_template}" > "${__ac_tmp}" 2>/dev/null; then
    echo "[oc:harden] FATAL: template whitelist merge failed" >&2
    rm -f "${__ac_tmp}" "${__ac_final}"
    return 1
  fi
  [ -s "${__ac_tmp}" ] || {
    rm -f "${__ac_tmp}" "${__ac_final}"
    return 1
  }

  if [ -n "${__ac_plan}" ]; then
    if ! oc_inject_config_from_plan \
        "${__ac_tmp}" "${__ac_plan}" "${__ac_scheme}" "${__ac_owner}" \
        "${__ac_region}" "${__ac_shared_vkey}" "${__ac_rsa}"; then
      rm -f "${__ac_tmp}" "${__ac_final}"
      return 1
    fi
  fi

  __ac_vkey="$(printf '%s' "${__ac_creds}" | jq -r '.litellm_vkey // ""' 2>/dev/null || true)"
  __ac_token="$(printf '%s' "${__ac_creds}" | jq -r '.gateway_token // ""' 2>/dev/null || true)"
  if ! oc_harden_config \
      "${__ac_tmp}" "${__ac_origin}" "${__ac_baseurl}" "${__ac_vkey}" \
      "${__ac_chat}"; then
    rm -f "${__ac_tmp}" "${__ac_final}"
    return 1
  fi

  if ! jq --arg token "${__ac_token}" --arg vkey "${__ac_vkey}" '
      (if $token != ""
       then setpath(["gateway", "auth", "token"]; $token)
       else . end)
      | (if $vkey != ""
         then setpath(["models", "providers", "litellm", "apiKey"]; $vkey)
         else . end)
    ' "${__ac_tmp}" > "${__ac_final}" 2>/dev/null; then
    echo "[oc:harden] FATAL: credential preservation failed" >&2
    rm -f "${__ac_tmp}" "${__ac_final}"
    return 1
  fi
  [ -s "${__ac_final}" ] || {
    rm -f "${__ac_tmp}" "${__ac_final}"
    return 1
  }
  mv -f "${__ac_final}" "${__ac_output}"
  rm -f "${__ac_tmp}"
  return 0
}
