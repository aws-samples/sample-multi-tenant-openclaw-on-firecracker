#!/bin/bash
# install.sh —— host 侧一键装 spire-kit(broker + 防火墙 + 反向路径过滤),纯新增
#
# 不改任何既有文件:launch-vm.sh / init-host.sh / host-agent.py / build-rootfs.sh
# 一行都不碰。落地的全是新文件:
#   /usr/local/bin/spire-join-broker.py
#   /etc/systemd/system/spire-join-broker.service
#   /etc/spire-kit/broker.env              (已存在则保留,不覆盖客户改过的配置)
#   /etc/sysctl.d/99-spire-kit-rpfilter.conf
#   /var/lib/spire-kit/ledger.json         (运行时台账)
# 加自有 iptables 规则(只针对 broker 端口):INPUT 里 tap+ 放行/其它入口 DROP,
# 外加 raw/PREROUTING 一条 `-m rpfilter --invert -j DROP` 做源地址伪造防护
# (不动 net.ipv4.conf.all,故不影响主 ENI 的非对称路由 —— 详见 apply_rpfilter 注释)。
#
# 用法(三个地址是必填,没有默认值 —— 见 validate_args 的注释):
#   sudo ./install.sh --registrar-url URL --trust-domain DOMAIN --spire-server-address ADDR
#                     [--port 8877] [--registrar-backend http|local|exec|stub]
#                     [--registrar-cmd PATH]        # exec 后端要调的客户插件
#                     [--spire-server-port 8081] [--spire-server-socket PATH]  # local 后端用
#                     [--force-env]                 # 按本次参数重写 broker.env(旧值备份 .bak)
#                     [--dry-run] [--no-firewall] [--no-rp-filter]
#                     [--harden-all-rp-filter]      # 额外把 conf/all 设 strict:会影响主 ENI,默认不做
#   sudo ./install.sh --status
#   sudo ./install.sh --check-images [--image-root /data/firecracker-assets]
#       挂载每个在役镜像(只读)报告有没有烤 kit;退出码 0=全有 1=有缺 3=判不出(fail-closed)。
#       治的是「新镜像没 kit → rebuild 后 VM 静默不再领证」这类安静失效。
#   sudo ./install.sh --check-attest [--max-attest-age 3600] [--ledger PATH] [--vm-root PATH]
#       领证间隔巡检:在跑的 VM 里谁长期没来领证(= kit 已静默消失);退出码 0=全新鲜 1=有 STALE/NEVER
#       3=判不出(台账缺失/损坏,常见于 broker 未装 —— 此时「没告警」不等于「正常」)。
#   sudo ./install.sh --uninstall
#
# 不想手动跑?用 hooks/host-user-hook.sh:它从 SSM Parameter Store 读上述配置,

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8877
BACKEND=""
REGISTRAR_URL=""
REGISTRAR_CMD=""
TRUST_DOMAIN=""
SERVER_ADDRESS=""
SERVER_PORT=""
SERVER_SOCKET=""
VM_ROOT=""
# #516 防呆① —— --check-images 的镜像搜索根。默认是真机实测的在役位置
# (usw2 metal 上 `openclaw-rootfs.ext4` / `openclaw-immutable.ext4` 都在这里);
IMAGE_ROOT="${SPIRE_KIT_IMAGE_ROOT:-/data/firecracker-assets}"
# #516 防呆② —— 领证间隔巡检:VM 在跑但长期没来领证 = 身份已经静默失效。
# 阈值给 3600s(broker 默认 token TTL 600s、每 boot 最多 3 次,正常 VM 开机后很快领完;
# 一小时还没有任何领证记录,就该有人去看)。ledger 是 broker 的运行时台账。
MAX_ATTEST_AGE="${SPIRE_KIT_MAX_ATTEST_AGE:-3600}"
LEDGER_FILE="${SPIRE_KIT_LEDGER:-/var/lib/spire-kit/ledger.json}"
DRY_RUN=0
DO_FIREWALL=1
DO_RPFILTER=1
HARDEN_ALL_RPFILTER=0
FORCE_ENV=0
ACTION="install"

BIN_DST="/usr/local/bin/spire-join-broker.py"
UNIT_DST="/etc/systemd/system/spire-join-broker.service"
ENV_DST="/etc/spire-kit/broker.env"
SYSCTL_DST="/etc/sysctl.d/99-spire-kit-rpfilter.conf"
NETGUARD_BIN="/usr/local/bin/spire-kit-netguard.sh"
NETGUARD_UNIT="/etc/systemd/system/spire-kit-netguard.service"

log() { echo "[spire-kit:install] $*"; }
run() {
  if [ "$DRY_RUN" = "1" ]; then echo "  DRY-RUN: $*"; else "$@"; fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?}"; shift 2 ;;
    --registrar-backend) BACKEND="${2:?}"; shift 2 ;;
    --registrar-url) REGISTRAR_URL="${2:?}"; shift 2 ;;
    --registrar-cmd) REGISTRAR_CMD="${2:?}"; shift 2 ;;
    --trust-domain) TRUST_DOMAIN="${2:?}"; shift 2 ;;
    --spire-server-address) SERVER_ADDRESS="${2:?}"; shift 2 ;;
    --spire-server-port) SERVER_PORT="${2:?}"; shift 2 ;;
    --spire-server-socket) SERVER_SOCKET="${2:?}"; shift 2 ;;
    --vm-root) VM_ROOT="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force-env) FORCE_ENV=1; shift ;;
    --no-firewall) DO_FIREWALL=0; shift ;;
    --no-rp-filter) DO_RPFILTER=0; shift ;;
    --harden-all-rp-filter) HARDEN_ALL_RPFILTER=1; shift ;;
    --status) ACTION="status"; shift ;;
    --check-images) ACTION="check-images"; shift ;;
    --image-root) IMAGE_ROOT="${2:?}"; shift 2 ;;
    --check-attest) ACTION="check-attest"; shift ;;
    --max-attest-age) MAX_ATTEST_AGE="${2:?}"; shift 2 ;;
    --ledger) LEDGER_FILE="${2:?}"; shift 2 ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

need_root() { [ "$(id -u)" = "0" ] || { echo "需要 root(sudo)" >&2; exit 1; }; }

validate_args() {
  # 这三个地址过去有内置默认值(127.0.0.1 / a1.1o / entry-registrar.spire.svc)。
  # 那是交付事故的温床:不传参数照样装得起来、healthz 照样绿、token 照样发,而 guest 拿着
  # server_address=127.0.0.1 去连自己的 8081,attestation 永远失败 —— 而且极难排查,
  # 因为 host 侧一路全绿。现在改成必填:配不全就拒装。
  #
  # 重装(升级 broker 二进制)时 write_env 会保留既有 broker.env、根本不看这些参数 ——
  # 那种情况下强制传参是假门槛,改为校验既有文件里的值,免得"升级必须重报一遍配置"。
  #
  # --force-env 例外:调用方声明"我就是配置的唯一来源"(hook 从 SSM 读值时用)。
  # 那种场景下必须走完整校验,否则 SSM 里改过的值会被既有文件默默盖掉。
  if [ -f "$ENV_DST" ] && [ "$ACTION" = "install" ] && [ "$FORCE_ENV" != "1" ]; then
    local have_td have_sa
    have_td="$(sed -n 's/^SPIRE_KIT_TRUST_DOMAIN=\(..*\)/\1/p' "$ENV_DST" | head -1)"
    have_sa="$(sed -n 's/^SPIRE_KIT_SERVER_ADDRESS=\(..*\)/\1/p' "$ENV_DST" | head -1)"
    if [ -n "$have_td" ] && [ -n "$have_sa" ]; then
      log "沿用既有 ${ENV_DST} 的配置(trust_domain / server_address 已就位),跳过参数必填校验"
      return
    fi
    echo "${ENV_DST} 存在但缺 SPIRE_KIT_TRUST_DOMAIN / SPIRE_KIT_SERVER_ADDRESS —— 请补全该文件或先 --uninstall" >&2
    exit 2
  fi
  local missing=()
  [ -n "$TRUST_DOMAIN" ]   || missing+=("--trust-domain")
  [ -n "$SERVER_ADDRESS" ] || missing+=("--spire-server-address")
  case "${BACKEND:-http}" in
    http)  [ -n "$REGISTRAR_URL" ] || missing+=("--registrar-url(backend=http 必填)") ;;
    local) : ;;   # 本机 spire-server,不经 registrar
    exec)  : ;;   # 客户插件自己知道去哪儿,URL 不适用
    stub)  : ;;   # 测试桩,另有 SPIRE_KIT_ALLOW_STUB 闸
    *) echo "--registrar-backend 只能是 http|local|exec|stub,收到 '${BACKEND}'" >&2; exit 2 ;;
  esac
  if [ ${#missing[@]} -gt 0 ]; then
    echo "缺必填参数:${missing[*]}" >&2
    echo "示例:sudo ./install.sh --registrar-url https://registrar.internal --trust-domain example.internal --spire-server-address spire.internal" >&2
    exit 2
  fi
  # guest 拿到 server_address 后是在【自己的 netns 里】连它。填 loopback 等于让 guest 连自己。
  case "$SERVER_ADDRESS" in
    127.0.0.1|localhost|::1)
      echo "--spire-server-address='${SERVER_ADDRESS}' 指向本机 —— guest 会去连自己,attestation 永远失败" >&2
      exit 2 ;;
  esac
}

firewall_rules() {
  # 顺序:先插兜底 DROP,再逐条插 ACCEPT(-I 1 让后插的落在 DROP 之上)。
  # 只针对 broker 端口:tap+(每 VM 的 tap)与 loopback 放行,其它入口(主 ENI /
  # 其它 host)一律 DROP。
  # loopback 那条不能省:兜底 DROP 不限接口,127.0.0.1 也走 INPUT —— 少了它,host 上
  # `curl 127.0.0.1:8877/healthz` 和 install.sh 自己的自检都会被自己的规则挡死
  # (真机 metal 上实测踩到)。lo 只有本机能用,放行不扩大暴露面。
  echo "DROP:-p tcp --dport ${PORT} -j DROP"
  echo "ACCEPT-TAP:-i tap+ -p tcp --dport ${PORT} -j ACCEPT"
  echo "ACCEPT-LO:-i lo -p tcp --dport ${PORT} -j ACCEPT"
}

apply_firewall() {
  [ "$DO_FIREWALL" = "1" ] || { log "跳过防火墙(--no-firewall)"; return; }
  command -v iptables >/dev/null 2>&1 || { log "WARN: 无 iptables,跳过防火墙"; return; }
  while IFS= read -r line; do
    kind="${line%%:*}"; rule="${line#*:}"
    # rule 是我们自己拼的规则片段,需要按空格拆成参数
    # shellcheck disable=SC2086
    if iptables -C INPUT ${rule} 2>/dev/null; then
      log "规则已存在(${kind}),跳过"
    else
      # shellcheck disable=SC2086
      run iptables -I INPUT 1 ${rule}
      log "已插入 ${kind} 规则:${rule}"
    fi
  done < <(firewall_rules)
}

remove_firewall() {
  command -v iptables >/dev/null 2>&1 || return 0
  while IFS= read -r line; do
    rule="${line#*:}"
    # shellcheck disable=SC2086
    while iptables -C INPUT ${rule} 2>/dev/null; do
      # shellcheck disable=SC2086
      run iptables -D INPUT ${rule} || break
    done
  done < <(firewall_rules)
  log "已清理 broker 端口相关 INPUT 规则"
}

rpfilter_match_rule() {
  # raw/PREROUTING 上的 strict 反向路径检查。DROP 掉"从 tapX 进来但源地址的最佳回程
  # 不是 tapX"的包 —— 也就是跨 tap 的源地址伪造。
  echo "-i tap+ -p tcp --dport ${PORT} -m rpfilter --invert -j DROP"
}

write_netguard() {
  # iptables 规则只活在运行时,reboot 全丢(2026-08-20 独立复现方真机 reboot 实撞:
  # broker 被 systemd 正常拉起,但 spoof_guard.present=false → enforce 拒发全部 token,
  # healthz ok:false)。首 boot 不踩是因为 init-host.sh 跑了 setup;reboot 不重跑 init。
  # 修法:oneshot unit 每次开机幂等重装这几条规则,broker unit 排在它之后。
  # 刻意不用 iptables-persistent/netfilter-persistent:那会把【整张表】存盘再恢复,
  # 连带固化平台/其它组件的运行时规则 —— 越权。本 unit 只管 kit 自己的 4 条。
  local tmp; tmp="$(mktemp)"
  {
    echo '#!/bin/bash'
    echo '# spire-kit-netguard.sh —— 开机幂等重装 broker 的 iptables 规则(install.sh 生成)'
    echo '# 规则与 install.sh 的 apply_firewall / apply_rpfilter 同源;改端口要重跑 install.sh。'
    echo 'set -u'
    echo "PORT=${PORT}"
    # INPUT 三条:先 DROP 兜底,ACCEPT 用 -I 1 压在其上(与 apply_firewall 同序)
    echo 'for spec in "-p tcp --dport ${PORT} -j DROP" "-i tap+ -p tcp --dport ${PORT} -j ACCEPT" "-i lo -p tcp --dport ${PORT} -j ACCEPT"; do'
    echo '  # shellcheck disable=SC2086'
    echo '  iptables -C INPUT ${spec} 2>/dev/null || iptables -I INPUT 1 ${spec}'
    echo 'done'
    # raw 一条:伪造防护主机制
    echo 'RP="-i tap+ -p tcp --dport ${PORT} -m rpfilter --invert -j DROP"'
    echo '# shellcheck disable=SC2086'
    echo 'iptables -t raw -C PREROUTING ${RP} 2>/dev/null || iptables -t raw -A PREROUTING ${RP} || {'
    echo '  echo "spire-kit-netguard: rpfilter 规则装不上(缺 xt_rpfilter?)—— broker enforce 下会拒发" >&2'
    echo '  exit 1'
    echo '}'
  } > "$tmp"
  run install -m 0755 "$tmp" "$NETGUARD_BIN"
  rm -f "$tmp"

  tmp="$(mktemp)"
  cat > "$tmp" << NETGUARDEOF
[Unit]
Description=spire-kit: reinstall broker iptables rules on boot (rules are runtime-only)
Documentation=file://${NETGUARD_BIN}
After=network-pre.target
Before=spire-join-broker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${NETGUARD_BIN}

[Install]
WantedBy=multi-user.target
NETGUARDEOF
  run install -m 0644 "$tmp" "$NETGUARD_UNIT"
  rm -f "$tmp"
  run systemctl enable spire-kit-netguard.service
  log "已装 netguard(开机重建 iptables 规则):${NETGUARD_BIN} + ${NETGUARD_UNIT}"
}

apply_rpfilter() {
  [ "$DO_RPFILTER" = "1" ] || { log "跳过伪造防护(--no-rp-filter);broker 默认 enforce 会因此拒发,请改 --rp-filter-policy warn 或自行加固"; return; }

  # ── 主机制:iptables raw 表的 rpfilter 匹配模块 ────────────────────────────
  # 为什么不靠 sysctl:rp_filter 的生效值是 max(conf/all, conf/<iface>)
  # (kernel.org ip-sysctl 原文),而 ClawPool host 的 /etc/sysctl.d/10-network-security.conf
  # 把 all 设成 2(loose)。于是"把每个 tap 设成 1"根本不生效:max(2,1)=2 仍是 loose,
  # 挡不住跨 tap 伪造(2026-08-18 真机 netns 实测:伪造包内核放行,零丢包计数)。
  # 而把 all 改成 1 会连带把主 ENI 切 strict,多 ENI / 策略路由的 host 上可能打断
  # 非对称路由 —— 那不是本 kit 能单方面替客户决定的,所以只作为 --harden-all-rp-filter 可选项。
  # rpfilter 匹配模块在【规则级】做同样的 strict 检查,不看 conf/all,且 -i tap+ 通配
  # 能自动覆盖之后新建的 tap(零改 launch-vm.sh 的前提下,per-tap 记账式规则对新 VM 会失效)。
  local rule_ok=0
  if command -v iptables >/dev/null 2>&1; then
    local rule; rule="$(rpfilter_match_rule)"
    # rule 是我们自己拼的规则片段,要按空格拆成参数,故刻意不加引号
    # shellcheck disable=SC2086
    rule_present() { iptables -t raw -C PREROUTING ${rule} 2>/dev/null; }
    # shellcheck disable=SC2086
    rule_install() { run iptables -t raw -A PREROUTING ${rule}; }
    if rule_present; then
      log "伪造防护规则已存在(raw/PREROUTING rpfilter),跳过"
      rule_ok=1
    elif rule_install; then
      log "已装伪造防护规则:iptables -t raw -A PREROUTING ${rule}"
      rule_ok=1
    else
      log "WARN: rpfilter 匹配模块不可用(内核缺 xt_rpfilter?),回落到 sysctl"
    fi
  else
    log "WARN: 无 iptables,无法装 rpfilter 规则,回落到 sysctl"
  fi

  # ── 兜底/叠加:per-tap sysctl ─────────────────────────────────────────────
  # 单独不足(见上),但保留:客户若自行把 conf/all 收成 1,这层就真的生效了;
  # 且它是 rpfilter 模块缺失时唯一的可用手段。
  run tee "$SYSCTL_DST" > /dev/null << 'SYSCTLEOF'
# spire-kit:反向路径过滤(叠加层,不是主机制)。
# 注意 rp_filter 的生效值是 max(conf/all, conf/<iface>) —— 若本机 conf/all 是 2(loose),
# 下面这行【不会】让 tap 变 strict。真正挡伪造的是 install.sh 装的
# `iptables -t raw ... -m rpfilter --invert -j DROP` 规则(不受 conf/all 影响)。
# 只设 default,不动 all,避免影响 host 主网卡的非对称路由。
net.ipv4.conf.default.rp_filter = 1
SYSCTLEOF
  run sysctl -q -w net.ipv4.conf.default.rp_filter=1 || true
  local n=0
  for path in /proc/sys/net/ipv4/conf/tap-vm*/rp_filter; do
    [ -e "$path" ] || continue
    iface="$(basename "$(dirname "$path")")"
    run sysctl -q -w "net.ipv4.conf.${iface}.rp_filter=1" || true
    n=$((n + 1))
  done

  # 可选加固:把 conf/all 也收成 strict。默认【不做】—— 会影响主 ENI。
  if [ "$HARDEN_ALL_RPFILTER" = "1" ]; then
    log "WARN: --harden-all-rp-filter:把 net.ipv4.conf.all.rp_filter 设为 1。这会让【主 ENI】也做 strict 反向路径检查;多 ENI / 策略路由 / 非对称路由的 host 上可能丢正常流量,请自行确认拓扑"
    run sysctl -q -w net.ipv4.conf.all.rp_filter=1 || true
    printf 'net.ipv4.conf.all.rp_filter = 1\n' | run tee -a "$SYSCTL_DST" > /dev/null
  fi

  local eff_all; eff_all="$(cat /proc/sys/net/ipv4/conf/all/rp_filter 2>/dev/null || echo '?')"
  log "伪造防护:iptables_rpfilter=$([ "$rule_ok" = 1 ] && echo present || echo ABSENT) sysctl(default+${n} 个既有 tap)=1 conf/all=${eff_all}(生效值取 max,故 all=2 时 sysctl 层无效)"

  if [ "$rule_ok" != 1 ] && [ "$eff_all" != "1" ]; then
    # 两条机制都不在位 → 说清后果,而不是留个假绿灯。broker enforce 下会拒发全部 token。
    log "ERROR: 伪造防护两条机制都不在位(无 rpfilter 规则、且 conf/all=${eff_all} 非 strict)。broker 在默认 --rp-filter-policy enforce 下会拒发全部 token;请装 xt_rpfilter、或加 --harden-all-rp-filter、或显式改用 --rp-filter-policy warn"
    return 1
  fi
}

remove_rpfilter_rule() {
  command -v iptables >/dev/null 2>&1 || return 0
  local rule; rule="$(rpfilter_match_rule)"
  # shellcheck disable=SC2086
  while iptables -t raw -C PREROUTING ${rule} 2>/dev/null; do
    # shellcheck disable=SC2086
    run iptables -t raw -D PREROUTING ${rule} || break
  done
}

write_env() {
  run install -d -m 0755 /etc/spire-kit
  # --force-env:调用方是配置的唯一来源(hook 从 SSM 读),必须覆盖。
  # 不给这个口子的后果在真机上踩到过:hook 第一次因别的原因失败、但已写下 broker.env,
  # 之后 hook 每次重跑都走"保留既有"分支,SSM 里改好的值被静默丢弃 —— 现象是
  # "参数明明改了、hook 明明跑了,broker 还在用旧配置",极难查。
  # 备份既有文件,免得运维手改过的值无痕消失。
  if [ -f "$ENV_DST" ] && [ "$FORCE_ENV" = "1" ]; then
    run cp -a "$ENV_DST" "${ENV_DST}.bak"
    log "--force-env:覆盖 ${ENV_DST}(旧值备份到 ${ENV_DST}.bak)"
  elif [ -f "$ENV_DST" ]; then
    log "保留既有 ${ENV_DST}(不覆盖客户改过的配置);要改配置请直接编辑该文件,或用 --force-env 强制按参数重写"
    return
  fi
  local tmp
  tmp="$(mktemp)"
  {
    echo "# spire-kit broker 配置(install.sh 生成;之后由客户自行编辑,升级不覆盖)"
    echo "SPIRE_KIT_PORT=${PORT}"
    echo "SPIRE_KIT_VM_ROOT=${VM_ROOT:-/data/firecracker-vms}"
    echo "SPIRE_KIT_REGISTRAR_BACKEND=${BACKEND:-http}"
    # 下面三个没有默认值 —— validate_args 已保证它们非空(见该函数注释:默认值会造成
    # "一路绿灯但 attestation 永远失败"的静默故障)。
    echo "SPIRE_KIT_REGISTRAR_URL=${REGISTRAR_URL}"
    echo "SPIRE_KIT_TRUST_DOMAIN=${TRUST_DOMAIN}"
    echo "SPIRE_KIT_SERVER_ADDRESS=${SERVER_ADDRESS}"
    echo "SPIRE_KIT_SERVER_PORT=${SERVER_PORT:-8081}"
    echo "SPIRE_KIT_WORKLOAD_UID=1000"
    echo "SPIRE_KIT_TOKEN_TTL=600"
    echo "SPIRE_KIT_MAX_ISSUES=3"
    echo "SPIRE_KIT_RP_FILTER_POLICY=enforce"
    echo "SPIRE_KIT_ENABLED=true          # 总开关:false 时领证一律 503,不影响 OpenClaw"
    if [ -n "$REGISTRAR_CMD" ]; then
      echo "SPIRE_KIT_REGISTRAR_CMD=${REGISTRAR_CMD}"
    else
      echo "#SPIRE_KIT_REGISTRAR_CMD=/etc/spire-kit/plugins/issue-token   # exec 后端要调的插件"
    fi
    [ -n "${SERVER_SOCKET}" ] && echo "SPIRE_KIT_SERVER_SOCKET=${SERVER_SOCKET}"
  } > "$tmp"
  run install -m 0644 "$tmp" "$ENV_DST"
  rm -f "$tmp"
  log "已写 ${ENV_DST}"
}

do_install() {
  # 参数校验排在 need_root 之前:它不需要权限,且先报"缺 --trust-domain"比先报
  # "需要 sudo"有用得多(否则配错参数的人先去加 sudo,再才看到真正的问题)。
  # 也让 --dry-run 能在非 root 环境验证参数组合。
  validate_args
  need_root
  command -v python3 >/dev/null 2>&1 || { echo "缺 python3" >&2; exit 1; }
  [ -f "${SELF_DIR}/spire-join-broker.py" ] || { echo "缺 broker 源文件" >&2; exit 1; }
  python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "${SELF_DIR}/spire-join-broker.py" \
    || { echo "broker 源文件语法检查失败" >&2; exit 1; }

  run install -m 0755 "${SELF_DIR}/spire-join-broker.py" "$BIN_DST"
  run install -m 0644 "${SELF_DIR}/spire-join-broker.service" "$UNIT_DST"
  run install -d -m 0700 /var/lib/spire-kit
  run install -d -m 0755 /etc/spire-kit/plugins   # 客户放自定义发证程序(exec 后端)的地方
  write_env
  apply_rpfilter
  apply_firewall
  write_netguard
  run systemctl daemon-reload
  run systemctl enable spire-join-broker.service
  # 刻意用 restart 而不是 `enable --now`:`--now` 对**已在运行**的服务是 no-op,它不重启。
  # 于是重入(升级 broker.py、改 broker.env)会变成"文件换了、进程还是旧的",而紧随其后的
  # 自检 curl 打的正是那个旧进程 —— 客户以为升级成功了,实际跑的还是旧代码旧配置。
  # 真机上踩过这条(#516 2026-08-17 apse1):broker.env 明明写着 /run/... 的 socket,
  # healthz 却一直报 /tmp/...,因为应答的是九分钟前起的旧进程。
  # restart 覆盖首装(未运行时等价于 start)与重入两种情况。
  run systemctl restart spire-join-broker.service
  if [ "$DRY_RUN" != "1" ]; then
    sleep 1
    # 断言应答的确实是刚起的进程,而不是某个还没被换掉的老进程。
    # 少了这条,上面那个 bug 会重新变成"静默"的。
    started="$(systemctl show spire-join-broker.service -p ExecMainStartTimestampMonotonic --value 2>/dev/null || echo 0)"
    envstamp="$(stat -c %Y "$ENV_DST" 2>/dev/null || echo 0)"
    now_mono="$(awk '{printf "%d", $1 * 1000000}' /proc/uptime 2>/dev/null || echo 0)"
    if [ "${started:-0}" -gt 0 ] && [ "${now_mono:-0}" -gt 0 ]; then
      age=$(( (now_mono - started) / 1000000 ))
      if [ "$age" -gt 60 ]; then
        log "WARN: broker 进程已运行 ${age}s,不像是刚被 restart 起来的 —— 可能仍在跑旧代码/旧配置"
        log "      手工确认:systemctl show spire-join-broker -p ExecMainStartTimestamp;stat -c %y ${ENV_DST}"
        exit 1
      fi
      log "进程新鲜度 OK(启动于 ${age}s 前,配置写于 $(date -d "@${envstamp}" '+%H:%M:%S' 2>/dev/null || echo '?'))"
    else
      # 拿不到值时【不能静默跳过】—— 那等于把这条断言变回它本来要治的那种静默失效
      # (第三轮复审抓出:原实现只有 if 分支,读不到就当没事发生,而"读不到"恰恰可能
      # 因为服务压根没起来)。改为回落到 ActiveState + 主进程存活的弱断言;弱断言也过不了
      # 才退出。旧版 systemd 的 --value 不支持、非 systemd 环境等情况都落在这里。
      log "WARN: 拿不到进程新鲜度基准(ExecMainStartTimestampMonotonic='${started}' /proc/uptime='${now_mono}')—— 回落到弱断言"
      state="$(systemctl is-active spire-join-broker.service 2>/dev/null || echo unknown)"
      mainpid="$(systemctl show spire-join-broker.service -p MainPID --value 2>/dev/null || echo 0)"
      if [ "$state" != "active" ] || [ "${mainpid:-0}" -le 0 ]; then
        log "ERROR: 弱断言也不过(is-active=${state} MainPID=${mainpid})—— broker 没在跑,不能报告安装成功"
        exit 1
      fi
      log "      弱断言过(is-active=active MainPID=${mainpid});但**无法证明应答者是刚起的进程**"
      log "      若这是一次升级,请手工确认:systemctl show spire-join-broker -p ExecMainStartTimestamp"
    fi
    if curl -sf --max-time 5 "http://127.0.0.1:${PORT}/healthz" > /tmp/spire-kit-healthz.$$ 2>/dev/null; then
      log "healthz: $(cat /tmp/spire-kit-healthz.$$)"
      # healthz 恒返回 HTTP 200(它报告状态,不用状态码表达状态),所以 curl -sf 成功
      # 只说明"进程在应答"。真正的判据是 body 里的 ok —— 它现在把 registrar 可达性
      # 也算进去了。registrar 不通时装完就报错,而不是留一个绿灯等第一台 VM 去踩。
      if grep -q '"ok": *true' /tmp/spire-kit-healthz.$$; then
        log "自检通过:broker 在应答且发证依赖可达"
      else
        log "WARN: broker 起来了,但 ok=false —— 发证依赖不可达(看上面的 registrar 字段)"
        log "      常见原因:registrar URL 填错 / 跨 VPC 路由或 SG 不通 / DNS 解析不了"
        rm -f /tmp/spire-kit-healthz.$$
        exit 1
      fi
      rm -f /tmp/spire-kit-healthz.$$
    else
      log "WARN: healthz 未通(看 journalctl -u spire-join-broker -n 50)"
      rm -f /tmp/spire-kit-healthz.$$
      exit 1
    fi
  fi
  log "安装完成。guest 侧装 kit:guest/install-guest-kit.sh --template <openclaw-data-template.ext4> --agent-binary <spire-agent>"
}

do_status() {
  echo "── service ──"
  systemctl is-enabled spire-join-broker.service 2>/dev/null || echo "(not enabled)"
  systemctl is-active spire-join-broker.service 2>/dev/null || echo "(not active)"
  echo "── healthz ──"
  curl -sf --max-time 3 "http://127.0.0.1:${PORT}/healthz" || echo "(unreachable)"
  echo
  echo "── iptables(broker 端口 ${PORT})──"
  iptables -S INPUT 2>/dev/null | grep -- "--dport ${PORT}" || echo "(no rules)"
  echo "── 伪造防护:主机制 = raw/PREROUTING rpfilter 规则 ──"
  iptables -t raw -S PREROUTING 2>/dev/null | grep -- "rpfilter" || echo "  (无 —— 伪造防护的主机制不在位!)"
  echo "── 伪造防护:sysctl 层(注意生效值是 max(all,iface))──"
  all_rpf="$(cat /proc/sys/net/ipv4/conf/all/rp_filter 2>/dev/null || echo '?')"
  echo "  conf/all = ${all_rpf}$([ "$all_rpf" = "2" ] && echo '  ← loose:per-tap 设成 1 也不生效,sysctl 这层等于没有')"
  for path in /proc/sys/net/ipv4/conf/tap-vm*/rp_filter; do
    [ -e "$path" ] || continue
    iface="$(basename "$(dirname "$path")")"; own="$(cat "$path")"
    eff="$own"; [ "${all_rpf}" != "?" ] && [ "${all_rpf}" -gt "$own" ] && eff="$all_rpf"
    echo "  ${iface}: file=${own} 生效=${eff}$([ "$eff" != "1" ] && echo ' (非 strict)')"
  done | head -5
  echo "── 总开关 ──"
  grep -E '^SPIRE_KIT_ENABLED=' "$ENV_DST" 2>/dev/null || echo "  (未显式设,默认 true)"
  echo "── 插件 ──"
  ls -1 /etc/spire-kit/plugins 2>/dev/null | sed 's/^/  /' || echo "  (无)"
  echo "── 台账 ──"
  [ -f /var/lib/spire-kit/ledger.json ] && wc -c < /var/lib/spire-kit/ledger.json | tr -d ' ' | sed 's/^/  ledger bytes: /' || echo "  (no ledger yet)"
}

platform_rpfilter_value() {
  # 平台自己声明的 net.ipv4.conf.<$1>.rp_filter。只认磁盘上的配置文件(排除本 kit 那份),
  # 不读运行时值 —— 运行时值此刻正是本 kit 写下的那个 1,拿它当"原值"会把错的固化下来。
  # 取最后一个匹配:systemd-sysctl 按文件名字典序应用、后者覆盖前者,所以最后一条最接近生效值。
  # 这只是近似(同文件内多次赋值、内核命令行覆盖都不在考虑内),所以取不到就不猜。
  local key="$1" f last="" v
  for f in /etc/sysctl.conf /etc/sysctl.d/*.conf /run/sysctl.d/*.conf /usr/lib/sysctl.d/*.conf; do
    [ -f "$f" ] || continue
    [ "$f" = "$SYSCTL_DST" ] && continue
    v="$(sed -n "s/^[[:space:]]*net\\.ipv4\\.conf\\.${key}\\.rp_filter[[:space:]]*=[[:space:]]*\\([0-9][0-9]*\\).*/\\1/p" "$f" | tail -1)"
    [ -n "$v" ] && last="$v"
  done
  printf '%s' "$last"
}

restore_all_rpfilter() {
  # 只在本 kit 确实动过 conf/all 时才回退(即安装时带过 --harden-all-rp-filter,
  # 痕迹留在 $SYSCTL_DST 里)。没动过就绝不碰 —— conf/all 关系到主 ENI 的非对称路由。
  [ -f "$SYSCTL_DST" ] || return 0
  grep -qE '^[[:space:]]*net\.ipv4\.conf\.all\.rp_filter[[:space:]]*=' "$SYSCTL_DST" || return 0
  local plat_all
  plat_all="$(platform_rpfilter_value all)"
  if [ -z "$plat_all" ]; then
    log "WARN: 本 kit 曾用 --harden-all-rp-filter 把 conf/all 设为 1,但磁盘配置没声明平台原值 —— 不猜值。"
    log "      运行时 conf/all 仍是 $(cat /proc/sys/net/ipv4/conf/all/rp_filter 2>/dev/null || echo '?');请手工确认后回退。"
    return 0
  fi
  run sysctl -q -w "net.ipv4.conf.all.rp_filter=${plat_all}" || true
  log "已回退 conf/all.rp_filter → ${plat_all}(平台声明值;本 kit 装时带过 --harden-all-rp-filter 才会走到这里)"
}

restore_rpfilter_sysctl() {
  # 删掉 $SYSCTL_DST 只是不再【开机】应用,已经写进内核的运行时值不会自己回去 ——
  # 于是卸载后 default 与全部 tap 停在 1,与平台 /etc/sysctl.d/10-network-security.conf
  # 声明的 2 不一致,而且没人看得见:--status 已经没了、日志说"已卸载"、下次重启才对上。
  # (#516 第五轮:真机卸载后逐条对快照复核时发现,default=1、100 个 tap 全 1。)
  # 恢复目标值取自平台配置文件而非本 kit 拍定;读不到就不猜、只报警,免得把 1 换成另一个错值。
  local plat_default plat_all
  plat_default="$(platform_rpfilter_value default)"
  plat_all="$(cat /proc/sys/net/ipv4/conf/all/rp_filter 2>/dev/null || echo '?')"
  if [ -z "$plat_default" ]; then
    log "WARN: 磁盘上的 sysctl 配置没有声明 net.ipv4.conf.default.rp_filter —— 不猜值。"
    log "      运行时仍是本 kit 设的 default=$(cat /proc/sys/net/ipv4/conf/default/rp_filter 2>/dev/null || echo '?'),tap 同理;重启后回到内核默认。要立刻回退请手工 sysctl -w。"
    return 0
  fi
  run sysctl -q -w "net.ipv4.conf.default.rp_filter=${plat_default}" || true
  local n=0 path iface
  for path in /proc/sys/net/ipv4/conf/tap-vm*/rp_filter; do
    [ -e "$path" ] || continue
    iface="$(basename "$(dirname "$path")")"
    run sysctl -q -w "net.ipv4.conf.${iface}.rp_filter=${plat_default}" || true
    n=$((n + 1))
  done
  log "已回退 rp_filter 运行时值:default 与 ${n} 个 tap → ${plat_default}(平台声明值);conf/all=${plat_all} 全程未动(装的时候也没动)"
}

do_uninstall() {
  need_root
  run systemctl disable --now spire-join-broker.service 2>/dev/null || true
  run systemctl disable --now spire-kit-netguard.service 2>/dev/null || true
  remove_firewall
  remove_rpfilter_rule
  # 两个回退必须排在 rm 之前:restore_all_rpfilter 要读 $SYSCTL_DST 才知道
  # 本 kit 当初有没有动过 conf/all(--harden-all-rp-filter 的痕迹只留在那份文件里)。
  # 文件删了就无从判断,只能要么不敢回退、要么盲改主 ENI 的设置 —— 两者都不行。
  restore_all_rpfilter
  restore_rpfilter_sysctl
  run rm -f "$BIN_DST" "$UNIT_DST" "$SYSCTL_DST" "$NETGUARD_BIN" "$NETGUARD_UNIT"
  run systemctl daemon-reload
  log "已卸载 broker(保留 ${ENV_DST} 与 /var/lib/spire-kit 台账,便于审计;要彻底清请手工 mv 到备份目录)"
}

# #516 防呆① —— 挂载 host 上每个在役镜像,报告「这个镜像里到底有没有 kit」。
#
# 为什么必须有这条:kit 有两种 guest 形态 —— 烤在 rootfs 里(`etc/spire-kit/`)或装在
# 数据盘家目录(`~/.spire-kit/`)。rebuild 换镜像版本时,如果新镜像**没**烤 kit,VM 照样
# 起得来、OpenClaw 照样跑,只是**再也不来领证** —— 身份静默消失,没有任何报错。同理
# restore 还原的是归档时刻的数据盘,归档早于装 kit 就把 kit 一起还原没了。两种都是
# 「安静失效」,靠事后看告警太晚,所以在换镜像/发布前先用这条把镜像点一遍。
#
# 只读:每个镜像 `mount -o loop,ro` 到临时目录,读完立刻 umount,绝不写镜像。
# 退出码(fail-closed):0 = 全部镜像都有 kit;1 = 至少一个没有(要么补烤要么明确接受);
#                      3 = 有镜像判不出来(挂不上/读不了)—— 不把「没看见」当「没问题」。
do_check_images() {
  need_root
  local total=0 with=0 without=0 undecided=0
  echo "── 镜像 kit 巡检(root=${IMAGE_ROOT})──"
  if [ ! -d "$IMAGE_ROOT" ]; then
    echo "  镜像根不存在:${IMAGE_ROOT}(用 --image-root 指定,或设 SPIRE_KIT_IMAGE_ROOT)" >&2
    return 3
  fi
  # 只认 rootfs / immutable 这两类会被 launch-vm 当根盘挂的镜像;数据盘(data.ext4)是
  # 租户私有数据,不在「镜像有没有烤 kit」这个问题的范围里,扫它只会产生噪音。
  local imgs; imgs="$(find "$IMAGE_ROOT" -maxdepth 3 -type f \
      \( -name '*rootfs*.ext4' -o -name '*immutable*.ext4' \) 2>/dev/null | sort)"
  if [ -z "$imgs" ]; then
    echo "  ${IMAGE_ROOT} 下没找到 *rootfs*.ext4 / *immutable*.ext4 —— 判不出,按 fail-closed 退 3" >&2
    return 3
  fi
  local img mp verdict
  while IFS= read -r img; do
    [ -n "$img" ] || continue
    total=$((total + 1))
    mp="$(mktemp -d)"
    if ! mount -o loop,ro "$img" "$mp" 2>/dev/null; then
      # 脏 ext4 日志会让 ro 挂载失败(已知形态),这里【不】改成 rw 重试:巡检绝不写镜像。
      verdict="UNDECIDED(挂载失败,可能是脏日志;巡检不写镜像故不 rw 重试)"
      undecided=$((undecided + 1))
    elif [ -f "$mp/etc/spire-kit/bootstrap.sh" ]; then
      verdict="KIT=PRESENT(rootfs 形态 etc/spire-kit/)"
      with=$((with + 1))
    elif compgen -G "$mp/root/.spire-kit/bootstrap.sh" >/dev/null 2>&1 \
      || compgen -G "$mp/home/*/.spire-kit/bootstrap.sh" >/dev/null 2>&1; then
      verdict="KIT=PRESENT(家目录形态 ~/.spire-kit/)"
      with=$((with + 1))
    else
      verdict="KIT=ABSENT —— 用这个镜像 rebuild 的 VM 会静默不再领证"
      without=$((without + 1))
    fi
    case "$verdict" in UNDECIDED*) : ;; *) umount "$mp" 2>/dev/null || true ;; esac
    rmdir "$mp" 2>/dev/null || true
    printf '  %-52s %s\n' "$(basename "$img")" "$verdict"
  done <<< "$imgs"
  echo "── 小计:镜像 ${total} / 有 kit ${with} / 无 kit ${without} / 判不出 ${undecided} ──"
  [ "$undecided" -gt 0 ] && return 3
  [ "$without" -gt 0 ] && return 1
  return 0
}

# #516 防呆② —— 领证间隔巡检。专治另一类「安静失效」:VM 起着、OpenClaw 跑着,但 kit 早就
# 没了(rebuild 换了没烤 kit 的镜像 / restore 还原了归档早于装 kit 的盘),于是它**再也不来
# 领证**,而没有任何东西会报错。防呆① 查的是「镜像里有没有 kit」(发布前),这条查的是
# 「在跑的 VM 到底还在不在领证」(运行中)——两条合起来才覆盖住那两个失效面。
#
# 判据:对每台**在跑**的 VM(vm.json 存在且无 .stopped 标记),看 broker 台账里它最近一次
# 领证时间。没有记录 = NEVER;超过 --max-attest-age = STALE。停机的 VM 不算(它本就不该领证)。
# 只读:只读 vm.json 与 ledger.json,不碰 broker、不写台账。
# 退出码(fail-closed):0 全部新鲜;1 有 STALE/NEVER;3 判不出(台账缺失/损坏、vm_root 不存在)
#   —— 台账缺失最常见的原因就是 broker 压根没装,那时候「没有告警」绝不等于「一切正常」。
do_check_attest() {
  need_root
  local vm_root="${VM_ROOT:-/data/firecracker-vms}"
  echo "── 领证间隔巡检(vm_root=${vm_root} ledger=${LEDGER_FILE} 阈值=${MAX_ATTEST_AGE}s)──"
  if [ ! -d "$vm_root" ]; then
    echo "  vm_root 不存在:${vm_root} —— 判不出(fail-closed)" >&2
    return 3
  fi
  if [ ! -f "$LEDGER_FILE" ]; then
    echo "  台账不存在:${LEDGER_FILE}" >&2
    echo "  → 判不出:broker 很可能没装/没跑过。此时「没有 STALE」不等于「都在领证」,故 fail-closed。" >&2
    echo "  → 先 ./install.sh --status 看 broker,或 --check-images 看镜像里有没有 kit。" >&2
    return 3
  fi
  command -v python3 >/dev/null 2>&1 || { echo "  缺 python3,无法解析台账 —— 判不出" >&2; return 3; }
  # 台账解析与判定放进 python3(bash 解 json 不可靠);它只读两个文件,不写任何东西。
  MAX_ATTEST_AGE="$MAX_ATTEST_AGE" LEDGER_FILE="$LEDGER_FILE" VM_ROOT_FOR_CHECK="$vm_root" python3 - <<'PYEOF'
import json, os, sys, time
from pathlib import Path

root = Path(os.environ["VM_ROOT_FOR_CHECK"])
ledger_path = Path(os.environ["LEDGER_FILE"])
max_age = int(os.environ["MAX_ATTEST_AGE"])
try:
    ledger = json.loads(ledger_path.read_text())
    if not isinstance(ledger, dict):
        raise ValueError(f"ledger root is {type(ledger).__name__}, expected object")
except Exception as exc:  # 坏台账 = 判不出,绝不当成"没问题"
    print(f"  台账损坏({exc!r})—— 判不出(fail-closed)", file=sys.stderr)
    sys.exit(3)

now = int(time.time())
running, stale, never, fresh, stopped = 0, 0, 0, 0, 0
rows = []
for meta in sorted(root.glob("*/vm.json")):
    vm_dir = meta.parent
    try:
        tenant_id = str(json.loads(meta.read_text())["tenant_id"])
    except Exception as exc:  # 单条坏记录不拖垮整轮巡检(与 broker 的 load_registry 同口径)
        rows.append((vm_dir.name, "SKIPPED(vm.json 不可解析: %r)" % (exc,)))
        continue
    if (vm_dir / ".stopped").exists():
        stopped += 1
        continue
    running += 1
    entry = ledger.get(tenant_id)
    if not isinstance(entry, dict):
        never += 1
        rows.append((tenant_id, "NEVER_ATTESTED —— 在跑却从未领证:kit 可能已随 rebuild/restore 消失"))
        continue
    last = entry.get("last_issued_at") or entry.get("last_claim_at")
    try:
        age = now - int(last)
    except (TypeError, ValueError):
        never += 1
        rows.append((tenant_id, "NEVER_ATTESTED —— 台账里没有可解析的领证时间"))
        continue
    if age > max_age:
        stale += 1
        rows.append((tenant_id, f"STALE —— 最近一次领证在 {age}s 前(阈值 {max_age}s)"))
    else:
        fresh += 1
        rows.append((tenant_id, f"FRESH —— {age}s 前领过证"))

for tid, verdict in rows:
    print("  %-40s %s" % (tid, verdict))
print(f"── 小计:在跑 {running} / 新鲜 {fresh} / STALE {stale} / 从未领证 {never} / 停机跳过 {stopped} ──")
sys.exit(1 if (stale or never) else 0)
PYEOF
}

case "$ACTION" in
  install) do_install ;;
  status) do_status ;;
  check-images) do_check_images ;;
  check-attest) do_check_attest ;;
  uninstall) do_uninstall ;;
esac
