#!/usr/bin/env bash
# 用真 openclaw 二进制(npm)+ 真 jq 注入代码,模拟「多个 firecracker 跑不同版本镜像 +
# 不同 openclaw.json」在 rebuild 重应用时的兼容性。产出可判别对照矩阵到 evidence 文件。
#
# 不碰任何 AWS / 真机租户;纯本地隔离 npm prefix + jq。可安全反复跑。
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"
SIM="/tmp/oc-version-compat-sim"
# OUT 默认落 /tmp:本脚本第一步就无条件 `: > "${OUT}"`,默认指向仓库内证据的话,
# 任何一次误跑(含带未识别参数的执行)都会把已提交的那份证据清空。要重新生成仓库里的
# 证据,显式传 OUT=engineering/evidence/429-version-compat-sim-<date>.md。
OUT="${OUT:-${SIM}/429-version-compat-sim.md}"
VERSIONS=("2026.2.26" "2026.6.11" "2026.7.1-2")
EXAMPLE="${REPO}/templates/openclaw.json.example"
# 有些版本的 Node 引擎下界高于本机 node(7.1-2 要 >=24.15.0)。指一个合规 node 的 bin 目录
# 进来,那些版本才测得动;不指就记 SKIPPED,不会伪装成 config 不兼容。
#   npm install --prefix /tmp/oc-node24 node@24
#   OC_NODE_BIN_DIR=/tmp/oc-node24/node_modules/.bin bash scripts/checks/oc-version-compat-sim.sh
NODE_DIR="${OC_NODE_BIN_DIR:-}"

mkdir -p "${SIM}"
: > "${OUT}"
log() { echo "$@" | tee -a "${OUT}"; }
raw() { echo "$@" >> "${OUT}"; }

log "# #429 跨版本 openclaw.json 兼容性真机模拟($(date -u '+%Y-%m-%dT%H:%M:%SZ'))"
log ""
log "真 openclaw 二进制(npm)+ 真 jq 注入代码。版本:${VERSIONS[*]}。基线模板:templates/openclaw.json.example。"
log ""

# 1) 隔离安装三个版本
for V in "${VERSIONS[@]}"; do
  D="${SIM}/v${V//./_}"
  BIN="${D}/node_modules/.bin/openclaw"
  if [ -x "${BIN}" ]; then
    log "- openclaw@${V} 已装(${BIN})"
    continue
  fi
  mkdir -p "${D}"
  log "- 安装 openclaw@${V} → ${D} ..."
  ( cd "${D}" && npm install --no-audit --no-fund --prefix "${D}" "openclaw@${V}" >"${D}/npm.log" 2>&1 )
  if [ -x "${BIN}" ]; then
    log "  OK $("${BIN}" --version 2>/dev/null | head -1)"
  else
    log "  FAIL 安装失败,见 ${D}/npm.log 末尾:"; tail -5 "${D}/npm.log" | sed 's/^/    /' | tee -a "${OUT}"
  fi
done

_bin_for() { echo "${SIM}/v${1//./_}/node_modules/.bin/openclaw"; }

# 二进制是否真能跑。openclaw 有 Node 引擎下界(7.1-2 要 >=24.15.0),不满足时
# 每次调用都在打印要求后立刻退出 —— 那种输出必须记 SKIPPED,不能当 config 不兼容。
_bin_runs() {  # binpath → 打印 "ok" 或 "node:<要求>" 或 "missing"
  local bin="$1" out
  [ -x "${bin}" ] || { echo "missing"; return; }
  out="$(PATH="${NODE_DIR}:$PATH" "${bin}" --version 2>&1)"
  if printf '%s' "${out}" | grep -q 'Node.js'; then
    echo "node:$(printf '%s' "${out}" | head -1 | cut -c1-90)"
  else
    echo "ok"
  fi
}

# 该版本有没有 config validate 子命令。2.26 的 config 只有 get/set/unset ——
# 对它跑 validate 得到的 "too many arguments" 是命令不存在,不是 schema 拒绝。
_has_validate() {  # binpath
  PATH="${NODE_DIR}:$PATH" "$1" config --help 2>&1 | grep -qE '^[[:space:]]+validate([[:space:]]|$)'
}

log ""
log "## A. 每个版本的可运行性与 config validate 支持度"
log ""
log "| 版本 | 二进制可运行 | config validate |"
log "|---|---|---|"
for V in "${VERSIONS[@]}"; do
  BIN="$(_bin_for "${V}")"
  R="$(_bin_runs "${BIN}")"
  case "${R}" in
    missing) log "| ${V} | 未安装 | — |"; continue;;
    node:*)  log "| ${V} | 否 — ${R#node:} | 无法探测 |"; continue;;
  esac
  if _has_validate "${BIN}"; then log "| ${V} | 是 | 有 |"; else log "| ${V} | 是 | 无(该版本 config 只有 get/set/unset) |"; fi
done
log ""
log "注:validate 校验的是 **active config**,不接受位置参数;本脚本用 \`OPENCLAW_CONFIG_PATH\` 指向被测变体,"
log "并以返回 JSON 里的 \`path\` 字段确认读到的就是该文件。node 引擎下界不满足时用 \`OC_NODE_BIN_DIR\` 指定合规 node。"

# 2) 交叉验证矩阵:用版本 X 的二进制 validate 基线模板(+ 各版本专属 key)
log ""
log "## B. 交叉兼容矩阵 — openclaw@<binver> config validate <config>"
log ""
log "config 变体:"
log "- base = templates/openclaw.json.example 原样"
log "- +v6key = base 加 6.11 新增键(heartbeat.isolatedSession/lightContext, compaction.midTurnPrecheck.enabled/maxActiveTranscriptBytes)"
log "- +v71key = base 加 7.1-2 新增键(plugins.entries.sentinel-guard.hooks.allowConversationAccess=true)"
log "- broken = **阴性对照**,故意把 model.primary 塞成数字 + 加一个不存在的顶层键。这一行必须 FAIL;"
log "  如果它也 PASS,说明 validate 根本没读到被测文件,同一轮里其它行的 PASS 全部不可用。"
log ""

CFG_BASE="${SIM}/cfg-base.json"
CFG_V6="${SIM}/cfg-v6key.json"
CFG_V71="${SIM}/cfg-v71key.json"
CFG_BAD="${SIM}/cfg-broken.json"
cp "${EXAMPLE}" "${CFG_BASE}" 2>/dev/null
# midTurnPrecheck 在 schema 里是 object {enabled:boolean},不是 boolean。写成 =true
# 会被判 Invalid input —— 那是变体构造错误,不是版本不兼容,别把它读成兼容性结论。
jq '.agents.defaults.heartbeat.isolatedSession=true
    | .agents.defaults.heartbeat.lightContext=true
    | .agents.defaults.compaction.midTurnPrecheck.enabled=true
    | .agents.defaults.compaction.maxActiveTranscriptBytes=1048576' "${CFG_BASE}" > "${CFG_V6}" 2>/dev/null
jq '.plugins.entries."sentinel-guard".hooks.allowConversationAccess=true' "${CFG_BASE}" > "${CFG_V71}" 2>/dev/null
jq '.agents.defaults.model.primary=12345 | .thisKeyDoesNotExistAtAll=true' "${CFG_BASE}" > "${CFG_BAD}" 2>/dev/null

validate_one() {  # binver cfgfile label
  local V="$1" CFG="$2"
  local BIN; BIN="$(_bin_for "${V}")"
  [ -f "${CFG}" ] || { echo "cfgmissing"; return; }
  local R; R="$(_bin_runs "${BIN}")"
  case "${R}" in
    missing) echo "SKIPPED(未安装)"; return;;
    node:*)  echo "SKIPPED(node 引擎不满足)"; return;;
  esac
  _has_validate "${BIN}" || { echo "N/A(无 validate 子命令)"; return; }
  local o rc
  o="$(PATH="${NODE_DIR}:$PATH" OPENCLAW_CONFIG_PATH="${CFG}" "${BIN}" config validate --json 2>&1)"; rc=$?
  # 判别力自证:JSON 的 path 必须回显被测文件,否则校验的是别的 config,结果不可用。
  if ! printf '%s' "${o}" | grep -qF "${CFG}"; then
    echo "INCONCLUSIVE(未确认读到被测文件)"; return
  fi
  if [ $rc -eq 0 ] && printf '%s' "${o}" | grep -q '"valid": *true'; then
    echo "PASS"
  else
    echo "FAIL: $(printf '%s' "${o}" | tr -d '\n' | grep -oE '"(message|code)": *"[^"]{0,80}"' | head -1)"
  fi
}

log "| config \\ openclaw binary | 2026.2.26 | 2026.6.11 | 2026.7.1-2 |"
log "|---|---|---|---|"
for pair in "base:${CFG_BASE}" "+v6key:${CFG_V6}" "+v71key:${CFG_V71}" "broken:${CFG_BAD}"; do
  LABEL="${pair%%:*}"; CFG="${pair#*:}"
  r1="$(validate_one 2026.2.26 "${CFG}")"
  r2="$(validate_one 2026.6.11 "${CFG}")"
  r3="$(validate_one 2026.7.1-2 "${CFG}")"
  log "| ${LABEL} | ${r1} | ${r2} | ${r3} |"
done

# 3) 真 jq 注入代码在每个 config 变体上的行为(host 侧 harden-config.sh)
log ""
log "## C. host 侧 jq 注入实测(oc_harden_config,真 jq)"
log ""
log "对每个 config 变体跑 oc_harden_config(注入 baseurl+vkey+chat=on),看是否 FATAL / 是否改到预期路径。"
log ""
HC="${REPO}/deploy/userdata/lib/harden-config.sh"
if [ -f "${HC}" ]; then
  # shellcheck disable=SC1090
  . "${HC}"
  log "| config | oc_harden_config rc | models.providers.litellm.apiKey 注入后 |"
  log "|---|---|---|"
  for pair in "base:${CFG_BASE}" "+v6key:${CFG_V6}" "+v71key:${CFG_V71}"; do
    LABEL="${pair%%:*}"; CFG="${pair#*:}"
    T="${SIM}/hc-${LABEL}.json"; cp "${CFG}" "${T}" 2>/dev/null
    oc_harden_config "${T}" "https://example.test" "http://gw:4000/v1" "sk-simkey-123" "true" >"${SIM}/hc-${LABEL}.err" 2>&1
    rc=$?
    injected="$(jq -r '.models.providers.litellm.apiKey // "<absent>"' "${T}" 2>/dev/null)"
    log "| ${LABEL} | ${rc} | ${injected} |"
  done
else
  log "- harden-config.sh 未找到:${HC}"
fi

log ""
log "## D. 关键结论(自动摘要,人工复核)"
log "- **先看 B 表 broken 行**:它不 FAIL,整张 B 表作废(说明 validate 没读到被测文件)。"
log "- B 表里只有 \`FAIL:\` 才是该版本 schema 真的拒绝这份 config → rebuild 重应用这种组合会让 gateway 起不来。"
log "  \`N/A\` = 该版本没有 validate 子命令,\`SKIPPED\` = 二进制在本机跑不起来(如 node 引擎下界),"
log "  \`INCONCLUSIVE\` = 没能确认读到被测文件。这三种都**不是**兼容性结论,不能当作拒绝。"
log "- 见上 C:jq 注入是否对任何变体 FATAL(rc≠0)。注入 FATAL 与 schema 拒绝是两种独立失败模式。"
log ""
log "_生成命令: OC_NODE_BIN_DIR=${NODE_DIR:-<未设,高 node 下界的版本会记 SKIPPED>} OUT=<路径> bash scripts/checks/oc-version-compat-sim.sh_"
log "_本机 node: $(node --version 2>/dev/null || echo 未知);OC_NODE_BIN_DIR 下的 node: $(PATH="${NODE_DIR}:$PATH" node --version 2>/dev/null || echo 未设)_"
echo "DONE → ${OUT}"
