#!/bin/bash
# spire-kit-acceptance.sh —— spire-kit 验收命令(逐条断言,退出码即结论)
#
# 分层,能跑哪层就跑哪层,跑不了的层【显式记 SKIP 并说清没证明什么】:
#   static  静态门:语法、shellcheck、无明文密钥、零侵入(不改任何【平台】既有文件)
#   logic   判定逻辑:身份矩阵 / 注册表 fail-closed / 一次性台账 / 限流 / 日志脱敏
#   http    真 HTTP 面:两个 loopback 地址模拟 /30 两端,验 200/403/409/429 与脱敏
#   shim    header 注入:假 agent + 假上游,验注入/剥伪造/fail-closed/日志脱敏
#   switch  可开关:--disabled 安装后 agent 不起、bootstrap 退 0、插件目录就位
#   netns   真链路(Linux+root):veth 当 tap、真源地址伪造防护(直读内核 DROP 计数)、跨租户实测
#   full    netns + 真 spire-server/spire-agent:真 attest、真 X.509/JWT-SVID
#
# 用法:
#   ./spire-kit-acceptance.sh                      # auto:能跑的都跑
#   ./spire-kit-acceptance.sh --tier static,logic  # 只跑指定层
#   sudo ./spire-kit-acceptance.sh --tier netns
#   sudo ./spire-kit-acceptance.sh --tier full --spire-bin-dir /usr/local/bin
#
# 结尾固定打印 `ASSERTIONS=<n> FAILED=<m> SKIPPED=<k>`;FAILED>0 或 ASSERTIONS=0 → 退出码 1。

set -uo pipefail
# 关掉 job 控制通知:cleanup 里 kill 后台进程时不再刷 "Terminated: 15" 噪声
set +m

ACC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(cd "${ACC_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${KIT_DIR}/../../.." 2>/dev/null && pwd || echo "")"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/spire-kit-acc.XXXXXX")"
TIERS="auto"
SPIRE_BIN_DIR=""
KEEP=0
PIDS=()
NETNS=()
VETHS=()
IPT_RULES=()          # "<table>|<chain>|<rule...>" —— cleanup 必须逐条撤,这套跑在【真 root netns】
SYSCTL_RESTORE=()     # "<key>=<原值>" —— 改过的内核开关要还原

ASSERTIONS=0
FAILED=0
SKIPPED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --tier) TIERS="${2:?}"; shift 2 ;;
    --spire-bin-dir) SPIRE_BIN_DIR="${2:?}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { ASSERTIONS=$((ASSERTIONS+1)); printf '  [PASS] %s\n' "$1"; }
bad()  { ASSERTIONS=$((ASSERTIONS+1)); FAILED=$((FAILED+1)); printf '  [FAIL] %s :: %s\n' "$1" "${2:-}"; }
skip() { SKIPPED=$((SKIPPED+1)); printf '  [SKIP] %s :: %s\n' "$1" "${2:-}"; }
hdr()  { printf '\n== %s ==\n' "$1"; }
assert_eq() { # name expected actual
  if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1" "expected=$2 actual=$3"; fi
}
assert_cmd() { # name cmd...
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name" "cmd failed: $*"; fi
}

cleanup() {
  # kill 后立刻 wait 回收:否则 bash 会在脚本收尾把 "Terminated: 15" 刷到 stderr,
  # 淹掉验收结论(真机上是通过 SSM 抓 stdout/stderr 的,噪声会让人误读)。
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  for ns in "${NETNS[@]:-}"; do [ -n "$ns" ] && ip netns del "$ns" 2>/dev/null; done
  for v in "${VETHS[@]:-}"; do [ -n "$v" ] && ip link del "$v" 2>/dev/null; done
  # iptables 规则与 sysctl 都跑在【真 root netns】里 —— 不撤干净会污染这台 host。
  for entry in "${IPT_RULES[@]:-}"; do
    [ -n "$entry" ] || continue
    t="${entry%%|*}"; rest="${entry#*|}"; c="${rest%%|*}"; r="${rest#*|}"
    # shellcheck disable=SC2086
    while iptables -t "$t" -C "$c" ${r} 2>/dev/null; do
      # shellcheck disable=SC2086
      iptables -t "$t" -D "$c" ${r} 2>/dev/null || break
    done
  done
  for entry in "${SYSCTL_RESTORE[@]:-}"; do
    [ -n "$entry" ] && sysctl -q -w "$entry" 2>/dev/null || true
  done
  if [ "$KEEP" = "1" ]; then echo "工作目录保留:$WORK"; else rm -rf "$WORK"; fi
}
# 装一条 iptables 规则并登记待撤。用法:ipt_add <table> <chain> <rule...>
ipt_add() {
  local t="$1" c="$2"; shift 2
  iptables -t "$t" -A "$c" "$@" || return 1
  IPT_RULES+=("${t}|${c}|$*")
}
# 改一个 sysctl 并登记原值待还原。用法:sysctl_set <key> <新值>
sysctl_set() {
  local key="$1" val="$2" old
  old="$(sysctl -n "$key" 2>/dev/null || echo '')"
  [ -n "$old" ] && SYSCTL_RESTORE+=("${key}=${old}")
  sysctl -q -w "${key}=${val}"
}
trap cleanup EXIT

want() { # tier 名是否要跑
  case ",${TIERS}," in
    *,auto,*) return 0 ;;
    *,"$1",*) return 0 ;;
    *) return 1 ;;
  esac
}

free_port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }

run_with_timeout() { # secs cmd... —— macOS 默认没有 timeout(1),自己兜一个
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
    return $?
  fi
  "$@" &
  local pid=$! rc=0
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
  local watch=$!
  wait "$pid"; rc=$?
  kill "$watch" 2>/dev/null
  return $rc
}

wait_http() { # url tries
  local url="$1" tries="${2:-40}"
  for _ in $(seq 1 "$tries"); do
    curl -sf --max-time 1 "$url" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  return 1
}

write_vm_json() { # dir tenant vm_num guest_ip
  mkdir -p "$1"
  printf '{"tenant_id":"%s","vm_num":%s,"guest_ip":"%s","vcpu":2,"mem_mb":2048,"config_template":"acc"}\n' \
    "$2" "$3" "$4" > "$1/vm.json"
  : > "$1/fc.sock"
}

echo "spire-kit acceptance"
echo "kit:        ${KIT_DIR}"
echo "work:       ${WORK}"
echo "tiers:      ${TIERS}"
echo "uname:      $(uname -s) $(uname -m)"
echo "python3:    $(python3 --version 2>&1)"
[ -n "$REPO_ROOT" ] && echo "repo:       ${REPO_ROOT}"

# ════════════════════════════════════════════════════════════════════════════
# static
# ════════════════════════════════════════════════════════════════════════════
if want static; then
  hdr "static(语法 / 脱敏 / 零侵入)"
  for f in spire-join-broker.py registrar-stub.py acceptance/logic_probe.py guest/spire-header-shim.py; do
    assert_cmd "py_compile ${f}" python3 -m py_compile "${KIT_DIR}/${f}"
  done
  # 排除 macOS AppleDouble 残渣(._*)与 __pycache__:它们不是脚本
  while IFS= read -r sh; do
    assert_cmd "bash -n $(basename "$sh")" bash -n "$sh"
  done < <(find "$KIT_DIR" -name '*.sh' -not -name '._*' -not -path '*/__pycache__/*' | sort)

  if command -v shellcheck >/dev/null 2>&1; then
    while IFS= read -r sh; do
      if shellcheck -S error -x "$sh" >"${WORK}/sc.out" 2>&1; then
        ok "shellcheck(error 级)$(basename "$sh")"
      else
        bad "shellcheck(error 级)$(basename "$sh")" "$(head -5 "${WORK}/sc.out" | tr '\n' ' ')"
      fi
    done < <(find "$KIT_DIR" -name '*.sh' -not -name '._*' -not -path '*/__pycache__/*' | sort)
  else
    skip "shellcheck" "本机无 shellcheck —— 未证明 shell 静态无 error 级问题"
  fi

  # 明文密钥面:kit 里不该出现任何看起来像凭据的东西
  # 排除本脚本自身:它含有的是"检测用的正则",不是凭据
  if grep -RInE '(AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|aws_secret_access_key|password *=)' \
      "$KIT_DIR" --exclude="$(basename "${BASH_SOURCE[0]}")" >"${WORK}/secrets.out" 2>/dev/null; then
    bad "kit 内无明文凭据" "$(head -3 "${WORK}/secrets.out")"
  else
    ok "kit 内无明文凭据"
  fi

  # 零侵入:kit 里不得有任何"写平台既有文件"的可执行语句(注释里引用它们是允许的)
  if grep -RInE '(sed +-i|tee +|>>?|install +|cp +|mv +|patch +)[^#]*(launch-vm\.sh|init-host\.sh|host-agent\.py|build-rootfs\.sh)' \
      "$KIT_DIR" --include='*.sh' --include='*.py' 2>/dev/null | grep -vE ':[0-9]+: *#' >"${WORK}/touch.out"; then
    bad "kit 不写平台既有文件" "$(head -3 "${WORK}/touch.out" | tr '\n' ' ')"
  else
    ok "kit 不写平台既有文件(仅注释里引用)"
  fi

  # 仓库层面的零侵入证据:本分支相对基线**没改动任何平台文件**。
  # 注意命题的口径:不是"零 M"。kit 自己的代码/文档/测试当然会被改(修 bug、补文档),
  # 那不是侵入;要守的是"平台既有文件(deploy/ cli/ build-rootfs.sh …)一个字节都没动"。
  # 基线 ref 不能写死远端名:本仓远端叫 origin,写死 gitlab/ 会让 merge-base 永远解不出来
  # → 这条断言静默 SKIP,和 I1 那个"拿不到基准就跳过"是同一类假绿灯。
  if [ -n "$REPO_ROOT" ] && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    BASE=""
    BASE_TRIED=""
    for ref in ${ACC_BASE_REF:-} origin/bb gitlab/bb bb; do
      BASE_TRIED="${BASE_TRIED}${ref} "
      BASE="$(git -C "$REPO_ROOT" merge-base HEAD "$ref" 2>/dev/null || echo "")"
      [ -n "$BASE" ] && break
    done
    if [ -n "$BASE" ]; then
      # kit 自己的地盘(改这些不算侵入);其余任何非 A 的改动都算动了平台文件。
      # #516 二期起有两个【获准的】平台集成点,一并放行:
      #   init-host.sh(Step 4c 装载段,SSM 闸关闭时零行为差异)、
      #   clawpool-deploy.sh(sync include *.service)。
      # 口径变化:一期"零侵入"= 平台一个字节不动(hook 通道);二期与平台侧对齐后
      # broker 走平台标准通道,"侵入"的定义收窄为"这两个点之外的平台文件不动"。
      # engineering/poc/spire-kit/ 是二期迁移前的旧址:git mv 的 rename 行会同时
      # 打出新旧两个路径,旧路径不放行会把自己的迁移误报成"动了平台文件"。
      KIT_OWNED='^(deploy/userdata/spire-kit/|engineering/poc/spire-kit/|deploy/userdata/init-host\.sh|engineering/deploy/clawpool-deploy\.sh|tests/test_spire_kit_|tests/test_516_spire_|engineering/SPIRE-|engineering/security/SPIRE-|engineering/security/spire-deck-sections\.py|engineering/00-knowledge-base/decisions/ADR-spire-|engineering/evidence/metal-experiments/spire-kit-|engineering/progress/spire-|engineering/changelog/ai-claude/spire-kit-)'
      PLATFORM_TOUCHED="$(git -C "$REPO_ROOT" diff --name-status "$BASE" HEAD 2>/dev/null \
        | awk '$1!="A"{ $1=""; sub(/^[ \t]+/,""); print }' \
        | tr -d '"' | grep -vE "$KIT_OWNED" || true)"
      if [ -z "$PLATFORM_TOUCHED" ]; then
        ok "相对基线零平台文件改动(基线 ${BASE:0:8};kit 自身文件改动不计)"
      else
        bad "相对基线零平台文件改动" "$(echo "$PLATFORM_TOUCHED" | head -3 | tr '\n' ' ')"
      fi
    else
      skip "相对基线零平台文件改动" "解不出基线 commit(试过:${BASE_TRIED}—— 用 ACC_BASE_REF=<ref> 指定)"
    fi
  else
    skip "相对基线零平台文件改动" "不在 git 仓库内(客户环境正常)"
  fi

  # guest kit 安装计划必须只列新增文件
  PLAN="$("${KIT_DIR}/guest/install-guest-kit.sh" --print-plan 2>&1)"
  if printf '%s' "$PLAN" | grep -q '\.config/systemd/user/default.target.wants/spire-agent.service'; then
    ok "guest kit 计划包含 user unit 自启软链"
  else
    bad "guest kit 计划包含 user unit 自启软链" "$(printf '%s' "$PLAN" | head -5 | tr '\n' ' ')"
  fi
  if printf '%s' "$PLAN" | grep -qE '^\s*[^+]*(openclaw\.json|openclaw-gateway\.service)'; then
    bad "guest kit 不碰 OpenClaw 自己的文件" "$PLAN"
  else
    ok "guest kit 不碰 OpenClaw 自己的文件"
  fi

  # 可开关 + 插件化(新要求):默认开、插件口存在、guest unit 有开关门
  assert_eq "broker 总开关默认开" "true" \
    "$(python3 "${KIT_DIR}/spire-join-broker.py" --print-config 2>/dev/null | sed -n 's/.*"enabled": *\([a-z]*\).*/\1/p')"
  if python3 "${KIT_DIR}/spire-join-broker.py" --help 2>/dev/null | grep -q 'exec=客户自己的程序'; then
    ok "broker 有 exec 插件后端(客户可整段换掉发证逻辑)"
  else
    bad "broker 有 exec 插件后端" "--help 里没看到 exec 说明"
  fi
  if grep -q 'SPIRE_KIT_TOKEN_CMD' "${KIT_DIR}/guest/spire-bootstrap.sh"; then
    ok "guest 有自定义取证插件口(SPIRE_KIT_TOKEN_CMD)"
  else
    bad "guest 有自定义取证插件口" "bootstrap 里没有"
  fi
  for unit in guest/spire-agent.service guest/spire-agent-system.service; do
    if grep -q 'ConditionPathExists=.*enabled' "${KIT_DIR}/${unit}"; then
      ok "$(basename "$unit") 带开关门(缺 enabled 标记就不启动)"
    else
      bad "$(basename "$unit") 带开关门" "缺 ConditionPathExists"
    fi
  done

  # 必填配置(交付期最贵的静默故障:一路绿灯但 attestation 永远失败)。
  # 三个环境相关地址过去有内置默认值,裸装会得到"broker 绿 + token 真发 + guest 连自己"。
  CFG0="$(python3 "${KIT_DIR}/spire-join-broker.py" --print-config 2>/dev/null)"
  for pair in 'trust_domain' 'spire_server_address' 'registrar_url'; do
    if printf '%s' "$CFG0" | grep -qE "\"${pair}\": *\"\""; then
      ok "${pair} 无内置默认值(必须显式配)"
    else
      bad "${pair} 无内置默认值" "$(printf '%s' "$CFG0" | grep "\"${pair}\"")"
    fi
  done
  # 配不全必须拒绝【启动】,而不是起来后静默发错身份
  if run_with_timeout 5 python3 "${KIT_DIR}/spire-join-broker.py" --port "$(free_port)" \
        >"${WORK}/cfg-gate.out" 2>&1; then
    bad "配置不全时拒绝启动" "无 trust-domain/server-address 也启动成功了"
  else
    if grep -q 'config_invalid\|配置不完整' "${WORK}/cfg-gate.out"; then
      ok "配置不全时拒绝启动(而非静默发错身份)"
    else
      bad "配置不全时拒绝启动" "$(head -3 "${WORK}/cfg-gate.out" | tr '\n' ' ')"
    fi
  fi
  # server_address 填 loopback 是最难排查的错配(guest 会去连自己)
  if run_with_timeout 5 python3 "${KIT_DIR}/spire-join-broker.py" --port "$(free_port)" \
        --trust-domain acc.spire.kit --spire-server-address 127.0.0.1 \
        --registrar-url http://127.0.0.1:1 >"${WORK}/loopback-gate.out" 2>&1; then
    bad "server-address 填 loopback 时拒绝启动" "127.0.0.1 也启动成功了"
  else
    ok "server-address 填 loopback 时拒绝启动"
  fi

  # host user hook(ASG 新 host 首 boot 自动装 broker,零改 ClawPool)
  HOOK="${KIT_DIR}/hooks/host-user-hook.sh"
  if [ -f "$HOOK" ]; then
    ok "host user hook 就位(hooks/host-user-hook.sh)"
    # IAM 关键约束:host role 只有 ssm:GetParameter(单数)。用复数 API 会 AccessDenied,
    # 那就得改 IAM,"零改 ClawPool"随之不成立 —— 这条守卫防止有人"顺手优化"成批量读。
    # 只看非注释行:注释里必须能提到这两个 API 才能说清"为什么不用它们"
    if grep -vE '^\s*#' "$HOOK" | grep -qE 'get-parameters(-by-path)?\b'; then
      bad "hook 只用 ssm get-parameter(单数)" "出现了复数 API,会 AccessDenied"
    else
      ok "hook 只用 ssm get-parameter(单数,host role 现有权限)"
    fi
    if grep -q 'ssm get-parameter ' "$HOOK"; then
      ok "hook 从 SSM 读配置(每环境不同地址,不写死在脚本里)"
    else
      bad "hook 从 SSM 读配置" "没看到 ssm get-parameter 调用"
    fi
    # hook 必须自己也 fail-closed:缺配置/校验不过就非 0 退出 → failure_policy: fail → ABANDON
    for guard in 'sha256sum -c' 'MISSING' 'die '; do
      if grep -q -- "$guard" "$HOOK"; then
        ok "hook 含 fail-closed 环节:${guard}"
      else
        bad "hook 含 fail-closed 环节:${guard}" "缺失"
      fi
    done
    # SSM 是 hook 的唯一配置来源,所以必须 --force-env 重写 broker.env。
    # 少了它,install.sh 默认走"保留既有 broker.env"分支:hook 一旦有过一次失败的
    # 部分安装(env 已写、后续步骤失败),之后每次重跑 SSM 的新值都被静默丢弃。
    # 真机上踩过这条(#516 2026-08-17 apse1)。
    if grep -q -- '--force-env' "$HOOK"; then
      ok "hook 用 --force-env 重写 broker.env(SSM 是唯一配置来源)"
    else
      bad "hook 用 --force-env" "缺失 —— SSM 改的值会被既有 broker.env 静默盖掉"
    fi
    # local 后端必须能传 socket 路径,否则 hook+local 结构上装不成
    if grep -q -- '--spire-server-socket' "$HOOK"; then
      ok "hook 能传 --spire-server-socket(local 后端必需)"
    else
      bad "hook 能传 --spire-server-socket" "缺失 —— backend=local 时 broker 只能用默认路径"
    fi
  else
    bad "host user hook 就位" "缺 hooks/host-user-hook.sh"
  fi

  # ── install.sh 的升级路径守卫 ──────────────────────────────────────────────
  # 注意这几条的【证明力等级】:下面 grep 的是源码文本,只能证明"代码里有这行字",
  # 不能证明运行时行为 —— 一次无害的重构就能绕过。真正的行为验证在 root 层用
  # `--dry-run` 跑真实代码路径(见紧随其后的 install-behavior 段),以及 full 层的真安装。
  # 保留 grep 是因为它在非 root / 无 Linux 的环境里也能跑,充当最低一档的回归网。
  if grep -qE '^\s*run systemctl restart spire-join-broker' "${KIT_DIR}/install.sh"; then
    ok "[静态] install.sh 源码含 systemctl restart(重入时真的换掉旧进程)"
  else
    bad "[静态] install.sh 源码含 systemctl restart" "没找到 —— enable --now 不会重启已运行的服务,升级会静默失效"
  fi
  if grep -qE 'run systemctl enable --now spire-join-broker' "${KIT_DIR}/install.sh"; then
    bad "[静态] install.sh 不用 enable --now 拉起" "出现了 enable --now:对已运行服务是 no-op,升级不生效"
  else
    ok "[静态] install.sh 不用 enable --now 拉起(避免 no-op 静默不升级)"
  fi
  if grep -q 'ExecMainStartTimestampMonotonic' "${KIT_DIR}/install.sh"; then
    ok "[静态] install.sh 源码含进程新鲜度断言"
  else
    bad "[静态] install.sh 源码含进程新鲜度断言" "缺失 —— 分不清是新进程应答还是旧进程还在应答"
  fi
  # 新鲜度断言拿不到基准值时【不能静默跳过】。老实现只有 if 分支,读不到就当没事发生 ——
  # 而"读不到"恰恰可能因为服务压根没起来。这条 grep 守住那个 else 分支的存在。
  if grep -q '弱断言也不过' "${KIT_DIR}/install.sh"; then
    ok "[静态] 新鲜度断言拿不到基准值时回落到弱断言并可能失败(不静默跳过)"
  else
    bad "[静态] 新鲜度断言不静默跳过" "缺 else 分支 —— 读不到基准值就跳过断言,等于把这个 bug 变回静默"
  fi
  # --uninstall 必须把 rp_filter 的【运行时值】也退回去。删掉 /etc/sysctl.d/99-spire-kit-rpfilter.conf
  # 只是不再开机应用,内核里已经写下的 1 会一直留着,直到重启 —— 而且没人看得见(--status 已经删了、
  # 日志说"已卸载")。#516 第五轮真机卸载后对快照复核时发现:default 与 100 个 tap 全停在 1,
  # 与平台 10-network-security.conf 声明的 2 不符。
  # 断言要求的是 do_uninstall 里【真的调了】它,不是"文件里出现过这个名字" ——
  # 只 grep 名字的话,函数定义留着、调用被删掉时断言照样绿(变异验证时确认过这个假绿)。
  if sed -n '/^do_uninstall() {/,/^}/p' "${KIT_DIR}/install.sh" | grep -q 'restore_rpfilter_sysctl'; then
    ok "[静态] do_uninstall 调用 rp_filter 运行时值回退(卸载不只删配置文件)"
  else
    bad "[静态] do_uninstall 调用 rp_filter 运行时值回退" "没调 —— 卸载后 default 与全部 tap 会一直停在 1 直到重启"
  fi
  # 顺序守卫:两个回退都必须排在 `rm -f … $SYSCTL_DST` 之前。restore_all_rpfilter 靠读那份文件
  # 判断本 kit 当初有没有动过 conf/all;文件先删了就只能要么不敢回退、要么盲改主 ENI 的设置。
  UNINST_RM_LN="$(grep -n 'run rm -f "\$BIN_DST"' "${KIT_DIR}/install.sh" | head -1 | cut -d: -f1)"
  UNINST_RESTORE_LN="$(grep -n '^  restore_all_rpfilter$' "${KIT_DIR}/install.sh" | head -1 | cut -d: -f1)"
  UNINST_RESTORE2_LN="$(grep -n '^  restore_rpfilter_sysctl$' "${KIT_DIR}/install.sh" | head -1 | cut -d: -f1)"
  if [ -n "$UNINST_RM_LN" ] && [ -n "$UNINST_RESTORE_LN" ] && [ -n "$UNINST_RESTORE2_LN" ] \
     && [ "$UNINST_RESTORE_LN" -lt "$UNINST_RM_LN" ] && [ "$UNINST_RESTORE2_LN" -lt "$UNINST_RM_LN" ]; then
    ok "[静态] 卸载时两个 rp_filter 回退都排在删 sysctl 配置文件之前"
  else
    bad "[静态] 卸载顺序:先回退再删配置文件" \
        "行号 restore_all=${UNINST_RESTORE_LN:-?} restore_default=${UNINST_RESTORE2_LN:-?} rm=${UNINST_RM_LN:-?} —— 文件先删就判不出当初动没动过 conf/all"
  fi

  # ── 行为验证:用 --dry-run 真跑 install.sh 的代码路径 ──────────────────────
  # 这一档比 grep 强:它真的执行 do_install 的分支、真的走 validate_args,只是把
  # 副作用命令打印出来而不执行。能抓到"grep 到了那行字但它在一个永远走不到的分支里"。
  if [ "$(id -u)" != "0" ]; then
    skip "install.sh --dry-run 行为验证" "非 root —— install.sh 要求 root,只能靠上面的静态守卫"
  else
    DRY_LOG="${WORK}/install-dryrun.log"
    if bash "${KIT_DIR}/install.sh" --dry-run --force-env \
         --registrar-backend local --trust-domain acc.dry.test \
         --spire-server-address 10.99.99.99 \
         --spire-server-socket /run/spire-server/private/api.sock \
         >"$DRY_LOG" 2>&1; then
      ok "install.sh --dry-run 正路退 0"
    else
      bad "install.sh --dry-run 正路退 0" "$(tail -3 "$DRY_LOG" | tr '\n' ' ')"
    fi
    # 真的走到了 restart 那一步(而不是只在源码里存在)
    if grep -q 'DRY-RUN: systemctl restart spire-join-broker' "$DRY_LOG"; then
      ok "[行为] --dry-run 实际执行到 systemctl restart 分支"
    else
      bad "[行为] --dry-run 实际执行到 systemctl restart 分支" "$(grep -c DRY-RUN "$DRY_LOG") 条 DRY-RUN 输出里没有 restart"
    fi
    # 真的走到了 rpfilter 规则那一步 —— 这是本轮伪造防护的主机制。
    # 两个分支都算走到:规则不在位 → 打印 DRY-RUN 的 -A;已在位 → 走幂等的"已存在,跳过"。
    # 只认前者是错的:apply_rpfilter 的 rule_present() 用真 `iptables -t raw -C`(读操作,
    # dry-run 下也执行,这是对的 —— 否则判不出在不在位),于是在【已装过 broker 的 host】上
    # 必然走"已存在"分支、永不打印 -A 行 → 断言假红。真机首次跑全层时踩到(#516 第五轮):
    # 本条是唯一的 FAIL,而 kit 完全正常。断言要判的是"代码路径走到了",不是"这次恰好新装"。
    if grep -qE 'DRY-RUN: iptables -t raw -A PREROUTING .*rpfilter --invert -j DROP' "$DRY_LOG"; then
      ok "[行为] --dry-run 走到装 rpfilter 伪造防护规则(规则原不在位 → 打印 -A)"
    elif grep -q '伪造防护规则已存在' "$DRY_LOG"; then
      ok "[行为] --dry-run 走到 rpfilter 伪造防护分支(规则已在位 → 幂等跳过)"
    else
      bad "[行为] --dry-run 走到 rpfilter 规则分支" \
          "两个分支都没出现:既无 DRY-RUN 的 -A,也无「已存在」—— 伪造防护代码路径根本没执行"
    fi
    # 默认【不】动 conf/all:那会把主 ENI 也切 strict
    if grep -qE 'DRY-RUN: sysctl -q -w net\.ipv4\.conf\.all\.rp_filter=1' "$DRY_LOG"; then
      bad "[行为] 默认不动 conf/all.rp_filter" "默认就改了 conf/all —— 会让主 ENI 做 strict 检查,可能打断非对称路由"
    else
      ok "[行为] 默认不动 conf/all.rp_filter(只有 --harden-all-rp-filter 才动)"
    fi
    # --harden-all-rp-filter 时才动,且必须带警告
    HARDEN_LOG="${WORK}/install-harden.log"
    bash "${KIT_DIR}/install.sh" --dry-run --force-env --harden-all-rp-filter \
      --registrar-backend local --trust-domain acc.dry.test \
      --spire-server-address 10.99.99.99 >"$HARDEN_LOG" 2>&1 || true
    if grep -qE 'DRY-RUN: sysctl -q -w net\.ipv4\.conf\.all\.rp_filter=1' "$HARDEN_LOG" \
       && grep -q '主 ENI' "$HARDEN_LOG"; then
      ok "[行为] --harden-all-rp-filter 才改 conf/all,且打印主 ENI 风险警告"
    else
      bad "[行为] --harden-all-rp-filter 改 conf/all 并警告" "没同时看到 sysctl all=1 与主 ENI 警告"
    fi
    # --force-env 真的走到覆盖分支(而不是"保留既有")
    if grep -qE 'force-env|覆盖' "$DRY_LOG"; then
      ok "[行为] --dry-run 走到 --force-env 覆盖分支"
    else
      skip "[行为] --force-env 覆盖分支" "本机没有既有 broker.env,该分支不触发(首装路径)"
    fi
    # 卸载路径的行为层:--uninstall --dry-run 必须真的走到 rp_filter 运行时回退。
    # 三种收场都算走到 —— 关键是这段代码执行了、而且它的判断有依据:
    #   ① 平台声明了值 → 打印 DRY-RUN 的 sysctl -w <平台值>;
    #   ② 磁盘上没人声明 → 明确报"不猜值"并说清运行时仍是几(诚实的不作为,不是静默跳过);
    # 只认 ① 会在不带 10-network-security.conf 的机器上假红。
    UNINST_LOG="${WORK}/install-uninstall-dryrun.log"
    bash "${KIT_DIR}/install.sh" --uninstall --dry-run >"$UNINST_LOG" 2>&1 || true
    if grep -qE 'DRY-RUN: sysctl -q -w net\.ipv4\.conf\.default\.rp_filter=[0-9]' "$UNINST_LOG"; then
      ok "[行为] --uninstall 走到 rp_filter 运行时回退(按平台声明值回退 default+tap)"
    elif grep -q '不猜值' "$UNINST_LOG"; then
      ok "[行为] --uninstall 走到 rp_filter 回退分支(磁盘无平台声明 → 拒绝猜值并报警,不静默)"
    else
      bad "[行为] --uninstall 走到 rp_filter 运行时回退" \
          "两种收场都没出现 —— 卸载只删了配置文件,内核里的 1 会一直留到重启:$(tail -3 "$UNINST_LOG" | tr '\n' ' ')"
    fi
    # 没装过 --harden-all-rp-filter 时,卸载【绝不能】碰 conf/all —— 那是主 ENI 的开关。
    if grep -qE 'DRY-RUN: sysctl -q -w net\.ipv4\.conf\.all\.rp_filter=' "$UNINST_LOG"; then
      if grep -q '本 kit 装时带过 --harden-all-rp-filter' "$UNINST_LOG"; then
        ok "[行为] --uninstall 回退 conf/all 仅因本机 sysctl 配置里确有 kit 写下的 all 记录"
      else
        bad "[行为] --uninstall 不擅自改 conf/all" "在没有 --harden-all-rp-filter 痕迹的情况下改了 conf/all —— 会动到主 ENI 的反向路径检查"
      fi
    else
      ok "[行为] --uninstall 默认不碰 conf/all(装的时候没动,卸载也不动)"
    fi
  fi

  # broker 默认配置自检
  CFG="$(python3 "${KIT_DIR}/spire-join-broker.py" --print-config 2>/dev/null)"
  assert_eq "默认 rp_filter 策略 enforce" '"enforce"' "$(printf '%s' "$CFG" | sed -n 's/.*"rp_filter_policy": *\("[a-z]*"\).*/\1/p')"
  assert_eq "默认 workload uid 1000" "1000" "$(printf '%s' "$CFG" | sed -n 's/.*"workload_uid": *\([0-9]*\).*/\1/p')"
  SHIMCFG="$(python3 "${KIT_DIR}/guest/spire-header-shim.py" --print-config 2>/dev/null)"
  assert_eq "shim 默认只听 loopback" '"127.0.0.1"' "$(printf '%s' "$SHIMCFG" | sed -n 's/.*"listen_host": *\("[0-9.]*"\).*/\1/p')"
  assert_eq "shim 默认校验上游 TLS" "false" "$(printf '%s' "$SHIMCFG" | sed -n 's/.*"upstream_insecure": *\([a-z]*\).*/\1/p')"
  assert_eq "shim 默认 on-missing=forward(不制造可用性悬崖)" '"forward"' "$(printf '%s' "$SHIMCFG" | sed -n 's/.*"on_missing": *\("[a-z]*"\).*/\1/p')"
  if SPIRE_KIT_ALLOW_STUB= run_with_timeout 5 python3 "${KIT_DIR}/spire-join-broker.py" --registrar-backend stub --port "$(free_port)" >"${WORK}/stub-gate.out" 2>&1; then
    bad "stub 后端必须显式开启" "无 SPIRE_KIT_ALLOW_STUB 也启动成功了"
  else
    if grep -q 'SPIRE_KIT_ALLOW_STUB' "${WORK}/stub-gate.out"; then
      ok "stub 后端未开 SPIRE_KIT_ALLOW_STUB 即拒绝启动"
    else
      bad "stub 后端未开 SPIRE_KIT_ALLOW_STUB 即拒绝启动" "$(head -3 "${WORK}/stub-gate.out" | tr '\n' ' ')"
    fi
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
# logic
# ════════════════════════════════════════════════════════════════════════════
if want logic; then
  hdr "logic(判定逻辑可执行断言)"
  if python3 "${ACC_DIR}/logic_probe.py" >"${WORK}/logic.out" 2>&1; then
    sed 's/^/  /' "${WORK}/logic.out" | grep -E 'ASSERT (ok|fail)' | sed 's/ASSERT ok /[PASS] /; s/ASSERT fail /[FAIL] /'
    n="$(sed -n 's/^TOTAL \([0-9]*\) FAILED \([0-9]*\)$/\1/p' "${WORK}/logic.out")"
    m="$(sed -n 's/^TOTAL \([0-9]*\) FAILED \([0-9]*\)$/\2/p' "${WORK}/logic.out")"
    ASSERTIONS=$((ASSERTIONS + ${n:-0}))
    FAILED=$((FAILED + ${m:-0}))
  else
    sed 's/^/  /' "${WORK}/logic.out" | tail -20
    n="$(sed -n 's/^TOTAL \([0-9]*\) FAILED \([0-9]*\)$/\1/p' "${WORK}/logic.out")"
    m="$(sed -n 's/^TOTAL \([0-9]*\) FAILED \([0-9]*\)$/\2/p' "${WORK}/logic.out")"
    ASSERTIONS=$((ASSERTIONS + ${n:-0}))
    FAILED=$((FAILED + ${m:-1}))
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
# http:两个 loopback 地址当 /30 两端
# ════════════════════════════════════════════════════════════════════════════
loopback_pair_ok() {
  python3 - <<'PY' >/dev/null 2>&1
import socket, sys
for ip in ("127.0.0.2", "127.0.0.3"):
    s = socket.socket()
    try:
        s.bind((ip, 0))
    except OSError:
        sys.exit(1)
    finally:
        s.close()
PY
}

if want http; then
  hdr "http(真 HTTP 面:200/403/409/429 + 日志脱敏)"
  if ! loopback_pair_ok; then
    skip "http 层" "本机不能用 127.0.0.2/127.0.0.3(macOS 需 ifconfig lo0 alias)—— 未证明真实 socket 面行为,请在 Linux 上跑 netns 层"
  else
    VM_ROOT="${WORK}/vms"
    write_vm_json "${VM_ROOT}/t-acc-a" "t-acc-a" 1 "127.0.0.3"   # host 端 127.0.0.2
    write_vm_json "${VM_ROOT}/t-acc-b" "t-acc-b" 2 "127.0.0.7"   # host 端 127.0.0.6
    REG_PORT="$(free_port)"
    BRK_PORT="$(free_port)"
    SPIRE_KIT_ALLOW_STUB=1 python3 "${KIT_DIR}/registrar-stub.py" --mode fake --bind 127.0.0.1 \
      --port "$REG_PORT" --trust-domain acc.spire.kit >"${WORK}/registrar.log" 2>&1 &
    PIDS+=("$!")
    SPIRE_KIT_ALLOW_STUB=1 python3 "${KIT_DIR}/spire-join-broker.py" \
      --bind 0.0.0.0 --port "$BRK_PORT" --vm-root "$VM_ROOT" \
      --registrar-backend http --registrar-url "http://127.0.0.1:${REG_PORT}" \
      --trust-domain acc.spire.kit --spire-server-address spire.acc.internal \
      --rp-filter-policy warn --max-issues-per-boot 1 --rate-limit-per-minute 5 \
      --state-file "${WORK}/ledger.json" --registry-ttl 0 >"${WORK}/broker.log" 2>&1 &
    PIDS+=("$!")

    if wait_http "http://127.0.0.1:${BRK_PORT}/healthz"; then
      ok "broker 起来了(healthz 可达)"
    else
      bad "broker 起来了(healthz 可达)" "$(tail -5 "${WORK}/broker.log" | tr '\n' ' ')"
    fi
    HEALTH="$(curl -s --max-time 2 "http://127.0.0.1:${BRK_PORT}/healthz")"
    assert_eq "healthz 认到 2 台 VM" "2" "$(printf '%s' "$HEALTH" | sed -n 's/.*"vms": *\([0-9]*\).*/\1/p')"

    # 正路:自己的 /30 两端
    CODE="$(curl -s -o "${WORK}/a.json" -w '%{http_code}' --interface 127.0.0.3 --max-time 3 \
      "http://127.0.0.2:${BRK_PORT}/v1/join-token")"
    assert_eq "自己的 /30 → 200" "200" "$CODE"
    TOKEN="$(sed -n 's/.*"join_token": *"\([^"]*\)".*/\1/p' "${WORK}/a.json")"
    if [ -n "$TOKEN" ]; then ok "拿到 join_token(len=${#TOKEN})"; else bad "拿到 join_token" "$(cat "${WORK}/a.json")"; fi
    assert_eq "token 绑对租户" "t-acc-a" "$(sed -n 's/.*"tenant_id": *"\([^"]*\)".*/\1/p' "${WORK}/a.json")"
    assert_eq "回传 trust_domain" "acc.spire.kit" "$(sed -n 's/.*"trust_domain": *"\([^"]*\)".*/\1/p' "${WORK}/a.json")"
    assert_eq "回传 server 坐标" "spire.acc.internal" "$(sed -n 's/.*"server_address": *"\([^"]*\)".*/\1/p' "${WORK}/a.json")"
    assert_eq "spiffe_id 带租户段" "spiffe://acc.spire.kit/openclaw/t-acc-a" \
      "$(sed -n 's/.*"spiffe_id": *"\([^"]*\)".*/\1/p' "${WORK}/a.json")"

    # 一次性:同一次开机再要 → 409
    CODE="$(curl -s -o "${WORK}/a2.json" -w '%{http_code}' --interface 127.0.0.3 --max-time 3 \
      "http://127.0.0.2:${BRK_PORT}/v1/join-token")"
    assert_eq "同一次开机重复要 → 409" "409" "$CODE"

    # 跨租户:B 的源地址打 A 的网关
    CODE="$(curl -s -o "${WORK}/x.json" -w '%{http_code}' --interface 127.0.0.7 --max-time 3 \
      "http://127.0.0.2:${BRK_PORT}/v1/join-token")"
    assert_eq "跨租户源地址 → 403" "403" "$CODE"
    # body 只给粗粒度原因(细原因能被用来扫"哪些 /30 存在"),细节只在 host 日志里
    assert_eq "403 body 不外泄细原因" "not_attested" \
      "$(sed -n 's/.*"reason": *"\([^"]*\)".*/\1/p' "${WORK}/x.json")"
    if grep -q '"reason": "src_ip_not_paired_guest"' "${WORK}/broker.log"; then
      ok "host 日志留了细原因(src_ip_not_paired_guest)"
    else
      bad "host 日志留了细原因" "$(grep -c denied "${WORK}/broker.log") 条 denied 但无该原因"
    fi

    # 未知目的地(不是任何 tap 网关)
    CODE="$(curl -s -o "${WORK}/y.json" -w '%{http_code}' --interface 127.0.0.3 --max-time 3 \
      "http://127.0.0.4:${BRK_PORT}/v1/join-token")"
    assert_eq "非 tap 网关目的地 → 403" "403" "$CODE"
    if grep -q '"reason": "dest_ip_not_a_tap_gateway"' "${WORK}/broker.log"; then
      ok "host 日志留了细原因(dest_ip_not_a_tap_gateway)"
    else
      bad "host 日志留了细原因(dest_ip_not_a_tap_gateway)" "日志里没找到"
    fi

    # 限流
    RL_HIT=0
    for _ in 1 2 3 4 5 6 7 8; do
      c="$(curl -s -o /dev/null -w '%{http_code}' --interface 127.0.0.7 --max-time 2 \
        "http://127.0.0.6:${BRK_PORT}/v1/join-token")"
      [ "$c" = "429" ] && RL_HIT=1 && break
    done
    assert_eq "疯狂重试触发 429" "1" "$RL_HIT"

    # 脱敏:token 明文不得出现在 broker / registrar 日志里
    if [ -n "$TOKEN" ] && grep -qF "$TOKEN" "${WORK}/broker.log" "${WORK}/registrar.log"; then
      bad "日志不含 token 明文" "token 明文出现在日志里"
    else
      ok "日志不含 token 明文(只有 sha256 前 8 位)"
    fi
    if grep -qE 'Traceback|UnboundLocalError' "${WORK}/broker.log"; then
      bad "broker 在拒绝路径上没抛异常" "$(grep -m1 -A3 Traceback "${WORK}/broker.log" | tr '\n' ' ')"
    else
      ok "broker 在拒绝路径上没抛异常(403/409 之后不炸)"
    fi
    if grep -q '"event": "issued"' "${WORK}/broker.log"; then
      ok "签发事件结构化留痕"
    else
      bad "签发事件结构化留痕" "$(tail -3 "${WORK}/broker.log" | tr '\n' ' ')"
    fi

    # guest 侧引导脚本(dry-run):真去取 token + 渲染 agent.conf
    GUEST_HOME="${WORK}/guest-home"
    mkdir -p "$GUEST_HOME"
    "${KIT_DIR}/guest/install-guest-kit.sh" --home "$GUEST_HOME" >"${WORK}/guest-install.log" 2>&1
    if [ -x "${GUEST_HOME}/.spire-kit/bootstrap.sh" ]; then ok "guest kit 装好(bootstrap 可执行)"; else bad "guest kit 装好" "$(tail -3 "${WORK}/guest-install.log")"; fi
    if [ -L "${GUEST_HOME}/.config/systemd/user/default.target.wants/spire-agent.service" ]; then
      ok "guest user unit 自启软链就位"
    else
      bad "guest user unit 自启软链就位" "missing symlink"
    fi
    # 换第三台租户(避开一次性台账),让 bootstrap 真取一次
    write_vm_json "${VM_ROOT}/t-acc-c" "t-acc-c" 3 "127.0.0.11"  # host 端 127.0.0.10
    sleep 0.2
    if HOME="$GUEST_HOME" SPIRE_KIT_DIR="${GUEST_HOME}/.spire-kit" \
       SPIRE_KIT_BROKER_HOST=127.0.0.10 SPIRE_KIT_BROKER_PORT="$BRK_PORT" \
       SPIRE_KIT_CURL_OPTS="--interface 127.0.0.11" SPIRE_KIT_MAX_RETRY=3 \
       SPIRE_KIT_BOOTSTRAP_DRY_RUN=1 "${GUEST_HOME}/.spire-kit/bootstrap.sh" >"${WORK}/bootstrap.log" 2>&1; then
      ok "guest bootstrap 取 token + 渲染配置成功"
    else
      bad "guest bootstrap 取 token + 渲染配置成功" "$(tail -5 "${WORK}/bootstrap.log" | tr '\n' ' ')"
    fi
    CONF="${GUEST_HOME}/.spire-kit/agent.conf"
    if [ -f "$CONF" ]; then
      if grep -q '{{' "$CONF"; then bad "agent.conf 无残留占位符" "$(grep -o '{{[A-Z_]*}}' "$CONF" | tr '\n' ' ')"; else ok "agent.conf 无残留占位符"; fi
      if grep -q 'trust_domain = "acc.spire.kit"' "$CONF"; then ok "agent.conf 的 trust_domain 来自 broker"; else bad "agent.conf 的 trust_domain 来自 broker" "$(grep trust_domain "$CONF")"; fi
      if grep -q 'server_address = "spire.acc.internal"' "$CONF"; then ok "agent.conf 的 server 坐标来自 broker"; else bad "agent.conf 的 server 坐标来自 broker" "$(grep server_address "$CONF")"; fi
      if grep -q 'KeyManager "memory"' "$CONF"; then ok "agent 私钥不落租户数据盘(KeyManager memory)"; else bad "agent 私钥不落租户数据盘" "$(grep -A1 KeyManager "$CONF" | tr '\n' ' ')"; fi
      if grep -qF "$TOKEN" "$CONF" 2>/dev/null; then bad "agent.conf 不含 join token" "token 被写进配置文件"; else ok "agent.conf 不含 join token(只经命令行一次性传入)"; fi
    else
      bad "agent.conf 生成" "文件不存在"
    fi
    # 二次 bootstrap(同一次开机)→ broker 409 → 脚本退 3,不是崩溃
    HOME="$GUEST_HOME" SPIRE_KIT_DIR="${GUEST_HOME}/.spire-kit" \
      SPIRE_KIT_BROKER_HOST=127.0.0.10 SPIRE_KIT_BROKER_PORT="$BRK_PORT" \
      SPIRE_KIT_CURL_OPTS="--interface 127.0.0.11" SPIRE_KIT_MAX_RETRY=1 \
      SPIRE_KIT_BOOTSTRAP_DRY_RUN=1 "${GUEST_HOME}/.spire-kit/bootstrap.sh" >"${WORK}/bootstrap2.log" 2>&1
    assert_eq "同一次开机二次引导退出码 3(409 语义)" "3" "$?"
  fi
fi


# ════════════════════════════════════════════════════════════════════════════
# shim:JWT-SVID header 注入(假 agent + 假上游,任何机器都能跑)
# ════════════════════════════════════════════════════════════════════════════
if want shim; then
  hdr "shim(JWT-SVID header 注入)"
  SHIM_DIR="${WORK}/shim"; mkdir -p "$SHIM_DIR"
  # 造一枚 exp 在未来的 JWT(只用于本地断言,不签名)
  FAKE_JWT="$(python3 - <<'PYJWT'
import base64, json, time
seg = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
print(f"{seg({'alg':'ES256'})}.{seg({'aud':['bgw'],'exp':int(time.time())+600,'sub':'spiffe://acc/openclaw/t-acc'})}.sig")
PYJWT
)"
  cat > "${SHIM_DIR}/fake-agent" <<AGENTEOF
#!/bin/bash
if [ -f "${SHIM_DIR}/agent-broken" ]; then echo "no identity issued" >&2; exit 1; fi
echo "token(spiffe://acc/openclaw/t-acc):"
printf '\t%s\n' "${FAKE_JWT}"
AGENTEOF
  chmod +x "${SHIM_DIR}/fake-agent"
  # 假上游:把收到的 header 逐行落盘
  cat > "${SHIM_DIR}/fake-upstream.py" <<'UPEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
OUT = sys.argv[2]
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): return
    def do_GET(self):
        with open(OUT, "a") as f:
            print(json.dumps({"path": self.path,
                              "headers": {k.lower(): v for k, v in self.headers.items()}}), file=f)
        body = b'{"ok":true}'
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    do_POST = do_GET
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
UPEOF
  UP_PORT="$(free_port)"; SHIM_PORT="$(free_port)"
  UP_LOG="${SHIM_DIR}/upstream.jsonl"; : > "$UP_LOG"
  python3 "${SHIM_DIR}/fake-upstream.py" "$UP_PORT" "$UP_LOG" >"${SHIM_DIR}/upstream.log" 2>&1 &
  PIDS+=("$!")
  python3 "${KIT_DIR}/guest/spire-header-shim.py" --listen "127.0.0.1:${SHIM_PORT}" \
    --upstream "http://127.0.0.1:${UP_PORT}" --agent-bin "${SHIM_DIR}/fake-agent" \
    --socket-path "${SHIM_DIR}/agent.sock" >"${SHIM_DIR}/shim.log" 2>&1 &
  PIDS+=("$!")
  UP_READY=0
  for _ in $(seq 1 40); do
    if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:${UP_PORT}/probe"; then UP_READY=1; break; fi
    sleep 0.25
  done
  assert_eq "假上游起来了" "1" "$UP_READY"
  : > "$UP_LOG"
  SHIM_UP=0
  for _ in $(seq 1 40); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:${SHIM_PORT}/probe")" = "200" ]; then
      SHIM_UP=1; break
    fi
    sleep 0.25
  done
  assert_eq "shim 起来了(且能打通上游)" "1" "$SHIM_UP"

  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${SHIM_PORT}/v1/chat")"
  assert_eq "经 shim 转发 → 200" "200" "$CODE"
  GOT="$(python3 - "$UP_LOG" <<'PYCHK'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
print(rows[-1]["headers"].get("x-spiffe-jwt-svid", "") if rows else "")
PYCHK
)"
  if [ "$GOT" = "$FAKE_JWT" ]; then ok "上游收到的 header 就是 agent 给的 JWT"; else bad "上游收到的 header 就是 agent 给的 JWT" "got=${GOT:0:24}…"; fi

  curl -s -o /dev/null --max-time 5 -H "X-SPIFFE-JWT-SVID: forged-by-client" \
    "http://127.0.0.1:${SHIM_PORT}/v1/chat" >/dev/null
  GOT2="$(python3 - "$UP_LOG" <<'PYCHK2'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
print(rows[-1]["headers"].get("x-spiffe-jwt-svid", ""))
PYCHK2
)"
  if [ "$GOT2" = "$FAKE_JWT" ]; then ok "客户端伪造的同名 header 被剥掉换成真 SVID"; else bad "客户端伪造的同名 header 被剥掉" "got=${GOT2:0:24}…"; fi

  if grep -qF "$FAKE_JWT" "${SHIM_DIR}/shim.log"; then
    bad "shim 日志不含 token 明文" "明文出现在日志里"
  else
    ok "shim 日志不含 token 明文(只有 sha256 前 8 位)"
  fi

  # fail-closed 档:agent 坏掉 + --on-missing reject → 503,且上游零请求
  : > "${SHIM_DIR}/agent-broken"
  REJ_PORT="$(free_port)"; REJ_LOG="${SHIM_DIR}/upstream-rej.jsonl"; : > "$REJ_LOG"
  REJ_UP="$(free_port)"
  python3 "${SHIM_DIR}/fake-upstream.py" "$REJ_UP" "$REJ_LOG" >/dev/null 2>&1 &
  PIDS+=("$!")
  python3 "${KIT_DIR}/guest/spire-header-shim.py" --listen "127.0.0.1:${REJ_PORT}" \
    --upstream "http://127.0.0.1:${REJ_UP}" --agent-bin "${SHIM_DIR}/fake-agent" \
    --socket-path "${SHIM_DIR}/agent.sock" --on-missing reject >"${SHIM_DIR}/shim-rej.log" 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:${REJ_UP}/probe" && break
    sleep 0.25
  done
  : > "$REJ_LOG"
  for _ in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:${REJ_PORT}/probe" >/dev/null 2>&1 && break
    sleep 0.25
  done
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${REJ_PORT}/v1/chat")"
  assert_eq "取不到 SVID + on-missing=reject → 503" "503" "$CODE"
  assert_eq "reject 档下上游零请求" "0" "$(grep -c . "$REJ_LOG" | tr -d ' ')"
  rm -f "${SHIM_DIR}/agent-broken"
fi

# ════════════════════════════════════════════════════════════════════════════
# switch:可开关(guest 侧标记 + host 侧总开关)
# ════════════════════════════════════════════════════════════════════════════
if want switch; then
  hdr "switch(可开关)"
  SW_HOME="${WORK}/switch-home"
  mkdir -p "$SW_HOME"
  "${KIT_DIR}/guest/install-guest-kit.sh" --home "$SW_HOME" --disabled >/dev/null 2>&1
  if [ -e "${SW_HOME}/.spire-kit/enabled" ]; then
    bad "--disabled 安装后没有 enabled 标记" "标记还在"
  else
    ok "--disabled 安装后没有 enabled 标记"
  fi
  if HOME="$SW_HOME" SPIRE_KIT_DIR="${SW_HOME}/.spire-kit" SPIRE_KIT_BROKER_HOST=127.0.0.1 \
     SPIRE_KIT_BOOTSTRAP_DRY_RUN=1 "${SW_HOME}/.spire-kit/bootstrap.sh" >"${WORK}/sw-off.log" 2>&1; then
    if grep -q '开关关闭' "${WORK}/sw-off.log"; then
      ok "开关关闭时 bootstrap 直接退 0(不取证、不起 agent)"
    else
      bad "开关关闭时 bootstrap 直接退 0" "$(tail -2 "${WORK}/sw-off.log" | tr '\n' ' ')"
    fi
  else
    bad "开关关闭时 bootstrap 直接退 0" "退出码非 0"
  fi
  touch "${SW_HOME}/.spire-kit/enabled"
  if [ -e "${SW_HOME}/.spire-kit/plugins" ]; then
    ok "guest 侧 plugins 目录就位(放自定义取证/钩子脚本)"
  else
    bad "guest 侧 plugins 目录就位" "缺目录"
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
# netns:真 veth 链路 + 真 rp_filter(Linux + root)
# ════════════════════════════════════════════════════════════════════════════
if want netns || want full; then
  hdr "netns(真链路 / 真 rp_filter / 源地址伪造实测)"
  if [ "$(uname -s)" != "Linux" ]; then
    skip "netns 层" "非 Linux —— 未证明真实 tap 链路、源地址伪造防护(内核 DROP 计数)与跨租户拒发"
  elif [ "$(id -u)" != "0" ]; then
    skip "netns 层" "非 root —— 需要 ip netns/veth/sysctl 权限"
  else
    VM_ROOT2="${WORK}/vms-netns"
    # tap-vm901: host 10.77.0.1/30  guest 10.77.0.2/30
    # tap-vm902: host 10.77.0.5/30  guest 10.77.0.6/30
    write_vm_json "${VM_ROOT2}/t-ns-a" "t-ns-a" 901 "10.77.0.2"
    write_vm_json "${VM_ROOT2}/t-ns-b" "t-ns-b" 902 "10.77.0.6"
    for n in 1 2; do
      ns="spirekit-g${n}"; tap="tap-vm90${n}"
      ip netns del "$ns" 2>/dev/null || true
      ip link del "$tap" 2>/dev/null || true
      ip netns add "$ns"; NETNS+=("$ns")
      ip link add "$tap" type veth peer name eth0 netns "$ns"; VETHS+=("$tap")
      if [ "$n" = 1 ]; then hip=10.77.0.1; gip=10.77.0.2; else hip=10.77.0.5; gip=10.77.0.6; fi
      ip addr add "${hip}/30" dev "$tap"; ip link set "$tap" up
      ip netns exec "$ns" ip addr add "${gip}/30" dev eth0
      ip netns exec "$ns" ip link set eth0 up
      ip netns exec "$ns" ip link set lo up
      ip netns exec "$ns" ip route add default via "$hip"
      sysctl_set "net.ipv4.conf.${tap}.rp_filter" 1
    done

    # ── 伪造防护:必须断言【生效值】,不是 per-iface 那个文件 ────────────────────
    # kernel.org ip-sysctl:生效值 = max(conf/all, conf/<iface>)。老断言只读 per-iface
    # 文件、读到 1 就报 strict —— 在 conf/all=2 的 host(ClawPool 的
    # 10-network-security.conf 就是 2)上那是【假绿灯】,伪造其实没被挡。
    ALL_RPF="$(cat /proc/sys/net/ipv4/conf/all/rp_filter 2>/dev/null || echo 0)"
    TAP_RPF="$(cat /proc/sys/net/ipv4/conf/tap-vm901/rp_filter 2>/dev/null || echo 0)"
    EFF_RPF="$TAP_RPF"; [ "$ALL_RPF" -gt "$TAP_RPF" ] && EFF_RPF="$ALL_RPF"
    echo "  [INFO] rp_filter: conf/all=${ALL_RPF} conf/tap-vm901=${TAP_RPF} 生效=max=${EFF_RPF}"
    if [ "$ALL_RPF" = "2" ] && [ "$EFF_RPF" = "2" ]; then
      ok "生效值断言有判别力:conf/all=2 时 per-tap=1 【不】等于 strict(生效=2)"
    fi

    REG_PORT2="$(free_port)"; BRK_PORT2="$(free_port)"

    # 装本 kit 真正的伪造防护:raw/PREROUTING 的 rpfilter 匹配规则(不依赖 conf/all)。
    # 这也让 netns 层测到的是【产品实际落地的机制】,而不是一个测试里临时造的条件。
    if ipt_add raw PREROUTING -i "tap+" -p tcp --dport "$BRK_PORT2" -m rpfilter --invert -j DROP 2>/dev/null; then
      ok "装上 rpfilter 匹配规则(raw/PREROUTING,伪造防护主机制)"
      RPF_RULE_ON=1
    else
      bad "装上 rpfilter 匹配规则" "iptables -m rpfilter 不可用(内核缺 xt_rpfilter?)—— 伪造防护主机制无法验证"
      RPF_RULE_ON=0
    fi
    SPIRE_KIT_ALLOW_STUB=1 python3 "${KIT_DIR}/registrar-stub.py" --mode fake --bind 127.0.0.1 \
      --port "$REG_PORT2" --trust-domain ns.spire.kit >"${WORK}/registrar-ns.log" 2>&1 &
    PIDS+=("$!")
    python3 "${KIT_DIR}/spire-join-broker.py" --bind 0.0.0.0 --port "$BRK_PORT2" \
      --vm-root "$VM_ROOT2" --registrar-backend http --registrar-url "http://127.0.0.1:${REG_PORT2}" \
      --trust-domain ns.spire.kit --spire-server-address 10.77.0.1 \
      --rp-filter-policy enforce --max-issues-per-boot 2 --registry-ttl 0 \
      --state-file "${WORK}/ledger-ns.json" >"${WORK}/broker-ns.log" 2>&1 &
    PIDS+=("$!")
    if wait_http "http://127.0.0.1:${BRK_PORT2}/healthz"; then ok "netns broker 起来了"; else bad "netns broker 起来了" "$(tail -5 "${WORK}/broker-ns.log" | tr '\n' ' ')"; fi

    CODE="$(ip netns exec spirekit-g1 curl -s -o "${WORK}/ns-a.json" -w '%{http_code}' --max-time 3 \
      "http://10.77.0.1:${BRK_PORT2}/v1/join-token")"
    assert_eq "guest1 经默认网关取 token → 200" "200" "$CODE"
    assert_eq "netns token 绑对租户" "t-ns-a" "$(sed -n 's/.*"tenant_id": *"\([^"]*\)".*/\1/p' "${WORK}/ns-a.json")"
    NS_TOKEN="$(sed -n 's/.*"join_token": *"\([^"]*\)".*/\1/p' "${WORK}/ns-a.json")"

    # 跨租户:guest2 打 guest1 的网关(host 本地地址走 INPUT,不受 FORWARD 超网 DROP 覆盖)
    CODE="$(ip netns exec spirekit-g2 curl -s -o "${WORK}/ns-x.json" -w '%{http_code}' --max-time 3 \
      "http://10.77.0.1:${BRK_PORT2}/v1/join-token")"
    assert_eq "guest2 打 guest1 网关 → 403" "403" "$CODE"
    if grep -q '"reason": "src_ip_not_paired_guest"' "${WORK}/broker-ns.log"; then
      ok "跨租户细原因只进 host 日志"
    else
      bad "跨租户细原因只进 host 日志" "日志里没找到"
    fi

    # ── 源地址伪造 ────────────────────────────────────────────────────────────
    # 老断言的毛病:只看"没换出 token"。那【分不清】是内核丢包还是应用层 403 —— 而注释
    # 却写着"内核直接丢包(不是应用层拒)",等于把一个没测过的性质写成了测过的。
    # 现在改成靠 iptables 规则的丢包计数器判定:计数增长 = 包真的在内核层被丢了。
    ip netns exec spirekit-g2 ip addr add 10.77.0.2/32 dev eth0 2>/dev/null || true
    ipt_drop_pkts() { # 本条 rpfilter 规则累计丢包数
      iptables -t raw -L PREROUTING -n -v -x 2>/dev/null \
        | awk -v p="$BRK_PORT2" '$0 ~ /rpfilter/ && $0 ~ ("dpt:" p) {s+=$1} END{print s+0}'
    }
    DROP_BEFORE="$(ipt_drop_pkts)"
    SPOOF_CODE="$(ip netns exec spirekit-g2 curl -s -o "${WORK}/ns-spoof.json" -w '%{http_code}' --max-time 3 \
      --interface 10.77.0.2 "http://10.77.0.1:${BRK_PORT2}/v1/join-token" 2>/dev/null || echo 000)"
    DROP_AFTER="$(ipt_drop_pkts)"
    DROP_DELTA=$(( DROP_AFTER - DROP_BEFORE ))
    # 判据一(必须成立):没换出 token。curl 在丢包时可能重试各打一个 000(实测见过 "000000")。
    if printf '%s' "$SPOOF_CODE" | grep -q '200'; then
      bad "源地址伪造未拿到 token" "HTTP ${SPOOF_CODE} body=$(tr -d '\n' < "${WORK}/ns-spoof.json" | head -c 120)"
    else
      ok "源地址伪造未拿到 token(HTTP ${SPOOF_CODE})"
    fi
    # 判据二(这才是"内核层挡住"的证据):丢包计数必须增长。
    if [ "$RPF_RULE_ON" = "1" ]; then
      if [ "$DROP_DELTA" -gt 0 ]; then
        ok "伪造包在【内核层】被丢(rpfilter 规则丢包 +${DROP_DELTA},不是应用层 403)"
      else
        bad "伪造包在内核层被丢" "rpfilter 规则丢包 +0 —— 包进到了应用层。若 HTTP=403 那是 broker 的 IP 配对检查拦的,不是反向路径过滤;'伪造被内核挡'这个结论【不成立】"
      fi
      # 对照:正常流量【不能】被这条规则误杀 —— 误杀=平台全挂,比漏更严重
      NORM_BEFORE="$(ipt_drop_pkts)"
      CODE="$(ip netns exec spirekit-g1 curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
        "http://10.77.0.1:${BRK_PORT2}/v1/join-token" 2>/dev/null || echo 000)"
      NORM_DELTA=$(( $(ipt_drop_pkts) - NORM_BEFORE ))
      if [ "$NORM_DELTA" = "0" ]; then
        ok "rpfilter 规则不误杀正常流量(丢包 +0;HTTP=${CODE},409 是一次性台账拦的,属预期)"
      else
        bad "rpfilter 规则不误杀正常流量" "正常请求也被丢了 +${NORM_DELTA} —— 这条规则会让平台全挂"
      fi
    else
      skip "伪造包在内核层被丢" "rpfilter 规则没装上,无法区分内核丢包与应用层拒"
    fi
    # 签发次数:只有 t-ns-a 该拿到证。上面"不误杀正常流量"那条又用 g1 打了一次,
    # 而 g1 的额度上限是 --max-issues-per-boot 2 —— 所以这里允许 1 或 2 次,但必须
    # 【全部属于 t-ns-a】:伪造与跨租户一次都不能换出 token。这条才是要守的性质。
    NS_ISSUED="$(grep -c '"event": "issued"' "${WORK}/broker-ns.log" || true)"
    NS_ISSUED_B="$(grep '"event": "issued"' "${WORK}/broker-ns.log" | grep -c '"tenant_id": "t-ns-b"' || true)"
    if [ -n "$NS_TOKEN" ] && [ "$NS_ISSUED_B" = "0" ] && [ "${NS_ISSUED:-0}" -ge 1 ]; then
      ok "签发全部属于 t-ns-a(共 ${NS_ISSUED} 枚);伪造与跨租户换出 token 次数 = 0"
    else
      bad "签发全部属于 t-ns-a" "issued=${NS_ISSUED} 其中 t-ns-b=${NS_ISSUED_B}(必须为 0)"
    fi
    ip netns exec spirekit-g2 ip addr del 10.77.0.2/32 dev eth0 2>/dev/null || true

    # ── fail-closed:伪造防护【两条机制都不在】时 enforce 必须拒发 ────────────────
    # 老版本只关 per-tap rp_filter 就断言 403。那在 conf/all=2 的 host 上是【碰巧过】的
    # 断言:关之前生效值本来就是 2(非 strict),关不关都该拒 —— 它证明不了"关掉防护会拒发"。
    # 现在必须把两条机制都撤掉才断言,并且撤 iptables 那条时顺便证明它原本在起作用。
    if [ "$RPF_RULE_ON" = "1" ]; then
      # shellcheck disable=SC2086
      iptables -t raw -D PREROUTING -i "tap+" -p tcp --dport "$BRK_PORT2" -m rpfilter --invert -j DROP
    fi
    sysctl_set net.ipv4.conf.tap-vm902.rp_filter 0
    CODE="$(ip netns exec spirekit-g2 curl -s -o "${WORK}/ns-rp.json" -w '%{http_code}' --max-time 3 \
      "http://10.77.0.5:${BRK_PORT2}/v1/join-token")"
    assert_eq "伪造防护两条机制都不在 → 403" "403" "$CODE"
    RP_REASON="$(sed -n 's/.*"reason": *"\([^"]*\)".*/\1/p' "${WORK}/ns-rp.json")"
    case "$RP_REASON" in
      spoof_guard_absent*) ok "403 原因是伪造防护缺失(${RP_REASON})" ;;
      *) bad "403 原因是伪造防护缺失" "got=${RP_REASON}(期望 spoof_guard_absent)" ;;
    esac
    if grep -q '"event": "spoof_guard_absent"' "${WORK}/broker-ns.log"; then
      ok "host 日志同时点出两条机制各自为什么不算"
    else
      bad "host 日志同时点出两条机制" "没找到 spoof_guard_absent 事件"
    fi
    # 反向确认:把 iptables 那条装回去,同一个请求就该能过了 —— 这证明【是它在起作用】,
    # 而不是别的东西碰巧让上面那条 403 出现。conf/all 全程没动过(仍是这台 host 的原值)。
    if [ "$RPF_RULE_ON" = "1" ]; then
      ipt_add raw PREROUTING -i "tap+" -p tcp --dport "$BRK_PORT2" -m rpfilter --invert -j DROP
      CODE="$(ip netns exec spirekit-g2 curl -s -o "${WORK}/ns-rp2.json" -w '%{http_code}' --max-time 3 \
        "http://10.77.0.5:${BRK_PORT2}/v1/join-token" 2>/dev/null || echo 000)"
      # 200(拿到)或 409(一次性台账已用完额度)都说明 attest 过了;403 说明没过。
      case "$CODE" in
        200|409) ok "装回 rpfilter 规则后 attest 恢复(HTTP ${CODE};conf/all 全程未改)" ;;
        *) bad "装回 rpfilter 规则后 attest 恢复" "HTTP ${CODE} —— 那上面那条 403 可能不是伪造防护缺失导致的" ;;
      esac
    fi
    sysctl_set net.ipv4.conf.tap-vm902.rp_filter 1

    # ────────────────────────────────────────────────────────────────────────
    # full:真 spire-server + 真 spire-agent(join_token attestation → 真 SVID)
    # ────────────────────────────────────────────────────────────────────────
    if want full; then
      SS="${SPIRE_BIN_DIR:-/usr/local/bin}/spire-server"
      SA="${SPIRE_BIN_DIR:-/usr/local/bin}/spire-agent"
      if [ ! -x "$SS" ] || [ ! -x "$SA" ]; then
        skip "full 层" "找不到 spire-server/spire-agent(--spire-bin-dir)—— 未证明真 attest 与真 SVID"
      else
        SRV_DIR="${WORK}/spire-server"
        mkdir -p "${SRV_DIR}/data"
        # 端口可覆盖,且先探冲突:客户 host 上完全可能已经跑着 spire-server 或别的东西占
        # 8081。硬编码 8081 时 bind 失败,后果是 full 层一连串 FAIL(真 attest、SVID、JWT
        # 全红),而真因只在 spire-server.log 里 —— 排查会以为是 kit 坏了。
        # 实测过一次(#516 2026-08-17 apse1:host 上已有 spire-server 占 8081 → 6 个级联 FAIL)。
        ACC_SERVER_PORT="${ACC_SPIRE_SERVER_PORT:-8081}"
        if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${ACC_SERVER_PORT}\b"; then
          for cand in 18081 18082 18083 18084; do
            if ! ss -ltn 2>/dev/null | grep -q ":${cand}\b"; then ACC_SERVER_PORT="$cand"; break; fi
          done
          echo "  [INFO] 8081 已被占用,临时 spire-server 改用 ${ACC_SERVER_PORT}(可用 ACC_SPIRE_SERVER_PORT 指定)"
        fi
        cat > "${SRV_DIR}/server.conf" << SRVEOF
server {
    bind_address = "0.0.0.0"
    bind_port = "${ACC_SERVER_PORT}"
    socket_path = "${SRV_DIR}/api.sock"
    trust_domain = "ns.spire.kit"
    data_dir = "${SRV_DIR}/data"
    log_level = "INFO"
    ca_ttl = "24h"
    default_x509_svid_ttl = "10m"
    default_jwt_svid_ttl = "5m"
}
plugins {
    DataStore "sql" {
        plugin_data {
            database_type = "sqlite3"
            connection_string = "${SRV_DIR}/data/datastore.sqlite3"
        }
    }
    NodeAttestor "join_token" { plugin_data {} }
    KeyManager "disk" { plugin_data { keys_path = "${SRV_DIR}/data/keys.json" } }
}
SRVEOF
        "$SS" run -config "${SRV_DIR}/server.conf" >"${WORK}/spire-server.log" 2>&1 &
        PIDS+=("$!")
        SRV_UP=0
        for _ in $(seq 1 40); do
          if "$SS" healthcheck -socketPath "${SRV_DIR}/api.sock" >/dev/null 2>&1; then SRV_UP=1; break; fi
          sleep 0.5
        done
        assert_eq "真 spire-server 起来了" "1" "$SRV_UP"

        # broker 换 registrar-stub 的 spire 模式(真 token generate + 真 entry create)
        REG_PORT3="$(free_port)"; BRK_PORT3="$(free_port)"
        # full 层的 broker 换了新端口,伪造防护规则必须【跟着这个端口】再装一条。
        # 漏了这一步的后果不是"少测一条",而是 full 层整体假故障:broker 的 spoof_guard
        # 按 --dport 精确匹配规则(本来就该这样 —— 规则不覆盖本端口就等于没防护),
        # 于是 enforce 下所有领证一律 403 spoof_guard_absent,bootstrap 5 次重试后 FATAL,
        # 后面 attest/SVID/JWT/重放全部级联红。真机首次跑全层时踩到(#516 第五轮),
        # 现场表象是"kit 坏了",真因只是验收脚本自己没给新端口装规则。
        if [ "$RPF_RULE_ON" = "1" ]; then
          ipt_add raw PREROUTING -i "tap+" -p tcp --dport "$BRK_PORT3" -m rpfilter --invert -j DROP \
            && ok "full 层 broker 端口也装上伪造防护规则(端口 ${BRK_PORT3})" \
            || bad "full 层 broker 端口装伪造防护规则" "装不上 —— full 层会整体 403 spoof_guard_absent"
        else
          skip "full 层 broker 端口装伪造防护规则" "netns 层就没装上(内核缺 xt_rpfilter?)"
        fi
        python3 "${KIT_DIR}/registrar-stub.py" --mode spire --bind 127.0.0.1 --port "$REG_PORT3" \
          --trust-domain ns.spire.kit --spire-server-bin "$SS" \
          --spire-server-socket "${SRV_DIR}/api.sock" >"${WORK}/registrar-real.log" 2>&1 &
        PIDS+=("$!")
        python3 "${KIT_DIR}/spire-join-broker.py" --bind 0.0.0.0 --port "$BRK_PORT3" \
          --vm-root "$VM_ROOT2" --registrar-backend http --registrar-url "http://127.0.0.1:${REG_PORT3}" \
          --trust-domain ns.spire.kit --spire-server-address 10.77.0.1 --spire-server-port "$ACC_SERVER_PORT" \
          --rp-filter-policy enforce --max-issues-per-boot 3 --registry-ttl 0 \
          --state-file "${WORK}/ledger-real.json" >"${WORK}/broker-real.log" 2>&1 &
        PIDS+=("$!")
        wait_http "http://127.0.0.1:${BRK_PORT3}/healthz" || true

        # guest1 里装 kit(真二进制)并按生产路径引导
        G1_HOME="${WORK}/g1-home"
        mkdir -p "$G1_HOME"
        "${KIT_DIR}/guest/install-guest-kit.sh" --home "$G1_HOME" --agent-binary "$SA" \
          >"${WORK}/g1-install.log" 2>&1
        if [ -x "${G1_HOME}/.spire-kit/bin/spire-agent" ]; then ok "真 spire-agent 已装进 guest kit"; else bad "真 spire-agent 已装进 guest kit" "$(tail -3 "${WORK}/g1-install.log")"; fi

        # 按生产身份跑:agent 与 workload 都是 uid 1000(registrar 建的 workload entry
        # selector 就是 unix:uid:1000)。用 root 去 fetch 会被 SPIRE 判 "no identity issued",
        # 那是测试跑法错,不是产品缺陷 —— 真机第三轮踩到过。
        chmod 755 "$WORK"
        chown -R 1000:1000 "$G1_HOME"
        AS_GUEST=(setpriv --reuid=1000 --regid=1000 --clear-groups)
        ip netns exec spirekit-g1 "${AS_GUEST[@]}" env HOME="$G1_HOME" \
          SPIRE_KIT_DIR="${G1_HOME}/.spire-kit" SPIRE_KIT_BROKER_PORT="$BRK_PORT3" \
          SPIRE_KIT_MAX_RETRY=5 "${G1_HOME}/.spire-kit/bootstrap.sh" >"${WORK}/g1-agent.log" 2>&1 &
        PIDS+=("$!")
        AGENT_UP=0
        for _ in $(seq 1 60); do
          if ip netns exec spirekit-g1 "${AS_GUEST[@]}" "$SA" api fetch x509 \
              -socketPath "${G1_HOME}/.spire-kit/run/agent.sock" >"${WORK}/x509.out" 2>&1; then
            AGENT_UP=1; break
          fi
          sleep 0.5
        done
        assert_eq "guest 内真 agent 完成 attestation 并拿到 X.509-SVID" "1" "$AGENT_UP"
        if [ "$AGENT_UP" != "1" ]; then
          echo "  ---- bootstrap/agent 日志(诊断用)----"
          tail -20 "${WORK}/g1-agent.log" 2>/dev/null | sed 's/^/    /'
          tail -10 "${G1_HOME}/.spire-kit/log/spire-agent.log" 2>/dev/null | sed 's/^/    /'
          # 把最常见的"测试环境自身没配好"与"产品真坏了"分开报,别让排查方向被带偏
          if grep -q 'spoof_guard_absent' "${WORK}/g1-agent.log" 2>/dev/null; then
            echo "  ---- 诊断:403 spoof_guard_absent ----" >&2
            echo "  full 层 broker 端口 ${BRK_PORT3} 上没有伪造防护规则 → enforce 下必然全拒。" >&2
            echo "  这是【验收环境】问题而非 kit 缺陷:当前 raw/PREROUTING 规则如下 ——" >&2
            iptables -t raw -S PREROUTING 2>/dev/null | sed 's/^/    /' >&2
          fi
        fi
        if grep -q 'spiffe://ns.spire.kit/openclaw/t-ns-a' "${WORK}/x509.out"; then
          ok "X.509-SVID 的 SPIFFE ID 就是本租户 workload 身份"
        else
          bad "X.509-SVID 的 SPIFFE ID 就是本租户 workload 身份" "$(head -6 "${WORK}/x509.out" | tr '\n' ' ')"
        fi
        if ip netns exec spirekit-g1 "${AS_GUEST[@]}" "$SA" api fetch jwt -audience bgw \
            -socketPath "${G1_HOME}/.spire-kit/run/agent.sock" >"${WORK}/jwt.out" 2>&1; then
          ok "JWT-SVID(audience=bgw)可取"
        else
          bad "JWT-SVID(audience=bgw)可取" "$(tail -4 "${WORK}/jwt.out" | tr '\n' ' ')"
        fi
        if grep -qE 'eyJ[A-Za-z0-9_-]{10,}' "${WORK}/jwt.out"; then
          ok "JWT-SVID 是真 JWT(三段式)"
        else
          bad "JWT-SVID 是真 JWT" "$(head -4 "${WORK}/jwt.out" | tr '\n' ' ')"
        fi
        # ── 重放:同一枚 join token 用两次 —— 第一次成功,第二次必须被 SPIRE 拒 ──
        CODE="$(ip netns exec spirekit-g2 curl -s -o "${WORK}/ns-real-b.json" -w '%{http_code}' --max-time 3 \
          "http://10.77.0.5:${BRK_PORT3}/v1/join-token")"
        RTOK="$(sed -n 's/.*"join_token": *"\([^"]*\)".*/\1/p' "${WORK}/ns-real-b.json")"
        if [ "$CODE" = "200" ] && [ -n "$RTOK" ]; then
          ok "guest2 也能领到自己的 token(HTTP 200)"
          for round in 1 2; do
            RP_DIR="${WORK}/replay-${round}"; mkdir -p "${RP_DIR}/state" "${RP_DIR}/run"
            sed -e "s|{{TRUST_DOMAIN}}|ns.spire.kit|; s|{{SERVER_ADDRESS}}|10.77.0.5|; s|{{SERVER_PORT}}|${ACC_SERVER_PORT}|" \
                -e "s|{{DATA_DIR}}|${RP_DIR}/state|; s|{{SOCKET_PATH}}|${RP_DIR}/run/agent.sock|; s|{{LOG_FILE}}|${RP_DIR}/agent.log|" \
                "${KIT_DIR}/guest/agent.conf.tmpl" > "${RP_DIR}/agent.conf"
            timeout 20 ip netns exec spirekit-g2 "$SA" run -config "${RP_DIR}/agent.conf" \
              -joinToken "$RTOK" >"${WORK}/replay-${round}.stdout" 2>&1
            # agent.conf 里配了 log_file,agent 的日志落文件而不是 stdout(真机第四轮踩到:
            # 只看 stdout 得到空日志,断言无从判定)。两处合并再判。
            cat "${WORK}/replay-${round}.stdout" "${RP_DIR}/agent.log" \
              > "${WORK}/replay-${round}.log" 2>/dev/null || true
          done
          if grep -qiE 'node attestation was successful|starting workload api' "${WORK}/replay-1.log"; then
            ok "第一次用该 token attest 成功(真 node attestation)"
          else
            bad "第一次用该 token attest 成功" "$(tail -3 "${WORK}/replay-1.log" | tr '\n' ' ')"
          fi
          if grep -qiE 'failed to attest|attestation failed|join token does not exist|token has already been used|unauthenticated' "${WORK}/replay-2.log"; then
            ok "同一枚 token 第二次 attest 被 SPIRE 拒(一次性成立)"
          else
            bad "同一枚 token 第二次 attest 被 SPIRE 拒" "$(tail -3 "${WORK}/replay-2.log" | tr '\n' ' ')"
          fi
        else
          bad "guest2 领 token 供重放测试" "HTTP ${CODE}"
        fi
      fi
    fi
  fi
fi

printf '\n──────────────────────────────────────────────\n'
printf 'ASSERTIONS=%s FAILED=%s SKIPPED=%s\n' "$ASSERTIONS" "$FAILED" "$SKIPPED"
if [ "$FAILED" -gt 0 ] || [ "$ASSERTIONS" -eq 0 ]; then
  echo "VERDICT=FAIL"
  exit 1
fi
echo "VERDICT=PASS"
exit 0
