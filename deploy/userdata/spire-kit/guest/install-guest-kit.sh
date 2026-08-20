#!/bin/bash
# install-guest-kit.sh —— 把 guest 侧 kit 装进【数据盘】,纯新增,不改任何既有文件
#
# 为什么装在数据盘而不是 rootfs:
#   · rootfs 由 build-rootfs.sh 烤,是镜像域的资产;改它 = 改别人的代码 + 出新镜像版本。
#   · 数据盘 /home/agent 上【已经】有 systemd user unit 的落点
#     (build-rootfs.sh:919-930 把 openclaw-gateway.service 写在
#      /home/agent/.config/systemd/user/default.target.wants/),
#     所以往同一目录再放一个 unit 就能开机自启,零改动。
#
# 目标(前两种装数据盘 user unit,后两种烤 rootfs 系统 unit):
#   --home <dir>            装进已挂载的 guest home(离线挂载的 data.ext4 或活体 guest)
#   --template <data.ext4>  Linux + root:loop 挂载模板盘 → 装入 → 卸载(给新租户用)
#   --rootfs <rootfs.ext4>  Linux + root:loop 挂载 rootfs 镜像 → 装系统级 unit → 卸载
#   --root-dir <dir>        装进已挂载的 rootfs(或活体 guest 的 /)
#   --print-plan            只打印将要新增的文件清单,不落地
#   --disabled              以关闭状态安装(不写 enabled 标记;agent/shim 都不启动)
#
# 两种形态二选一,不要同时装:数据盘版 uid 1000 跑、零镜像改动;rootfs 版 root 跑、
# 每台 VM 一装到底(客户文档里的形态,需要出新镜像版本)。
#
# 幂等:重复跑只覆盖 kit 自己的文件;发现同名但非本 kit 的文件时拒绝覆盖(除非 --force)。

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=""
TARGET=""
AGENT_BIN=""
FORCE=0
ENABLED=1
AGENT_USER="agent"
AGENT_UID=1000
AGENT_GID=1000

usage() {
  sed -n '2,32p' "${BASH_SOURCE[0]}"
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --home) MODE="home"; TARGET="${2:?--home 需要目录}"; shift 2 ;;
    --template) MODE="template"; TARGET="${2:?--template 需要 data.ext4 路径}"; shift 2 ;;
    --rootfs) MODE="rootfs"; TARGET="${2:?--rootfs 需要 rootfs.ext4 路径}"; shift 2 ;;
    --root-dir) MODE="rootdir"; TARGET="${2:?--root-dir 需要目录}"; shift 2 ;;
    --print-plan) MODE="plan"; shift ;;
    --agent-binary) AGENT_BIN="${2:?--agent-binary 需要路径}"; shift 2 ;;
    --agent-user) AGENT_USER="${2:?}"; shift 2 ;;
    --agent-uid) AGENT_UID="${2:?}"; shift 2 ;;
    --agent-gid) AGENT_GID="${2:?}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --disabled) ENABLED=0; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 2 ;;
  esac
done

log() { echo "[spire-kit:install-guest] $*"; }

KIT_FILES=(
  ".spire-kit/bootstrap.sh"
  ".spire-kit/agent.conf.tmpl"
  ".spire-kit/header-shim.py"
  ".spire-kit/shim.env.example"
  ".spire-kit/README"
  ".config/systemd/user/spire-agent.service"
  ".config/systemd/user/default.target.wants/spire-agent.service"
  ".config/systemd/user/spire-header-shim.service"
  ".config/systemd/user/default.target.wants/spire-header-shim.service"
)

ROOTFS_FILES=(
  "etc/spire-kit/bootstrap.sh"
  "etc/spire-kit/agent.conf.tmpl"
  "etc/spire-kit/header-shim.py"
  "etc/spire-kit/shim.env.example"
  "etc/spire-kit/README"
  "etc/systemd/system/spire-agent.service"
  "etc/systemd/system/multi-user.target.wants/spire-agent.service"
  "etc/systemd/system/spire-header-shim.service"
  "etc/systemd/system/multi-user.target.wants/spire-header-shim.service"
)

if [ "$MODE" = "plan" ] || [ -z "$MODE" ]; then
  log "数据盘形态(--home/--template)将【新增】:"
  for f in "${KIT_FILES[@]}"; do echo "  + \$HOME/$f"; done
  [ -n "$AGENT_BIN" ] && echo "  + \$HOME/.spire-kit/bin/spire-agent  (来自 $AGENT_BIN)"
  echo "  + \$HOME/.spire-kit/{state,run,log}/  (空目录)"
  log "rootfs 形态(--rootfs/--root-dir)将【新增】:"
  for f in "${ROOTFS_FILES[@]}"; do echo "  + /$f"; done
  [ -n "$AGENT_BIN" ] && echo "  + /usr/local/bin/spire-agent  (来自 $AGENT_BIN)"
  echo "  + /var/lib/spire-kit/  (空目录;运行时 /run/spire 由 systemd RuntimeDirectory 建)"
  [ "$MODE" = "plan" ] && exit 0
  usage 2
fi

install_into_rootfs() {
  local root="$1"
  [ -d "$root" ] || { echo "目标 rootfs 目录不存在: $root" >&2; exit 1; }
  [ -d "${root}/etc/systemd/system" ] || { echo "$root 看着不像 rootfs(缺 etc/systemd/system)" >&2; exit 1; }

  for f in "etc/spire-kit/bootstrap.sh" "etc/systemd/system/spire-agent.service" \
           "etc/spire-kit/header-shim.py" "etc/systemd/system/spire-header-shim.service"; do
    if [ -e "${root}/${f}" ] && [ "$FORCE" != "1" ]; then
      grep -q 'spire-kit' "${root}/${f}" 2>/dev/null || {
        echo "拒绝覆盖非本 kit 的同名文件: ${root}/${f}(要覆盖请加 --force)" >&2; exit 1; }
    fi
  done

  install -d -m 0755 "${root}/etc/spire-kit" "${root}/usr/local/bin" \
    "${root}/etc/systemd/system/multi-user.target.wants" "${root}/var/lib/spire-kit" \
    "${root}/etc/spire-kit/plugins"
  if [ "$ENABLED" = "1" ]; then
    : > "${root}/etc/spire-kit/enabled"
  else
    rm -f "${root}/etc/spire-kit/enabled"
    log "以【关闭】状态烤入(缺 enabled 标记):该镜像起的 VM 不会启动 agent/shim"
  fi
  install -m 0755 "${SELF_DIR}/spire-bootstrap.sh" "${root}/etc/spire-kit/bootstrap.sh"
  install -m 0644 "${SELF_DIR}/agent.conf.tmpl" "${root}/etc/spire-kit/agent.conf.tmpl"
  install -m 0644 "${SELF_DIR}/spire-agent-system.service" "${root}/etc/systemd/system/spire-agent.service"
  install -m 0755 "${SELF_DIR}/spire-header-shim.py" "${root}/etc/spire-kit/header-shim.py"
  install -m 0644 "${SELF_DIR}/shim.env.example" "${root}/etc/spire-kit/shim.env.example"
  install -m 0644 "${SELF_DIR}/spire-header-shim-system.service" "${root}/etc/systemd/system/spire-header-shim.service"
  # 开机自启:等价于 systemctl enable(镜像里不能跑 systemctl,直接放 wants 软链)
  ln -sfn "../spire-agent.service" "${root}/etc/systemd/system/multi-user.target.wants/spire-agent.service"
  # shim unit 带 ConditionPathExists=/etc/spire-kit/shim.env —— 没配就不启动,出网行为零变化
  ln -sfn "../spire-header-shim.service" "${root}/etc/systemd/system/multi-user.target.wants/spire-header-shim.service"

  cat > "${root}/etc/spire-kit/README" << 'ROOTREADME'
spire-kit(guest 侧 · rootfs 形态)—— per-microVM SPIRE 身份

开机流程:systemd → spire-agent.service → /etc/spire-kit/bootstrap.sh
  1. 找默认网关(= 本 VM /30 的 host 端)
  2. GET http://<gw>:8877/v1/join-token  取一次性 join token
  3. 渲染 /run/spire/agent.conf(server 坐标由 broker 回传)
  4. exec /usr/local/bin/spire-agent run -joinToken <token>

Workload API socket:/run/spire/agent.sock(OpenClaw uid 1000 直接连)
自检:systemctl status spire-agent · journalctl -u spire-agent -n 50 --no-pager
      spire-agent api fetch jwt -audience bgw -socketPath /run/spire/agent.sock
ROOTREADME

  if [ -n "$AGENT_BIN" ]; then
    [ -f "$AGENT_BIN" ] || { echo "spire-agent 二进制不存在: $AGENT_BIN" >&2; exit 1; }
    install -m 0755 "$AGENT_BIN" "${root}/usr/local/bin/spire-agent"
    log "baked spire-agent binary ($(stat -c%s "$AGENT_BIN" 2>/dev/null || stat -f%z "$AGENT_BIN") bytes)"
  else
    log "WARN: 未提供 --agent-binary,kit 已装但没有 spire-agent —— bootstrap 会 fail-closed 退出并记日志"
  fi

  log "installed into rootfs ${root}"
  for f in "${ROOTFS_FILES[@]}"; do
    [ -e "${root}/${f}" ] && echo "  ok  /${f}" || echo "  MISSING /${f}"
  done
}

mount_and_install() { # <ext4> <installer-fn>
  local img="$1" fn="$2"
  [ "$(id -u)" = "0" ] || { echo "$MODE 需要 root(loop 挂载)" >&2; exit 1; }
  [ -f "$img" ] || { echo "镜像不存在: $img" >&2; exit 1; }
  MNT="$(mktemp -d /tmp/spire-kit-img.XXXXXX)"
  cleanup() { umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true; }
  trap cleanup EXIT
  mount -o loop "$img" "$MNT"
  "$fn" "$MNT"
  sync
  cleanup
  trap - EXIT
}

install_into_home() {
  local home="$1"
  [ -d "$home" ] || { echo "目标 home 不存在: $home" >&2; exit 1; }

  # 冲突检查:同名文件存在且不是本 kit 装的 → 停,不覆盖别人的东西
  for f in ".spire-kit/bootstrap.sh" ".config/systemd/user/spire-agent.service" \
           ".spire-kit/header-shim.py" ".config/systemd/user/spire-header-shim.service"; do
    if [ -e "${home}/${f}" ] && [ "$FORCE" != "1" ]; then
      if ! grep -q 'spire-kit' "${home}/${f}" 2>/dev/null; then
        echo "拒绝覆盖非本 kit 的同名文件: ${home}/${f}(要覆盖请加 --force)" >&2
        exit 1
      fi
    fi
  done

  install -d -m 0755 "${home}/.spire-kit" "${home}/.spire-kit/bin" \
    "${home}/.config/systemd/user/default.target.wants"
  install -d -m 0700 "${home}/.spire-kit/state" "${home}/.spire-kit/run" "${home}/.spire-kit/log"
  install -d -m 0755 "${home}/.spire-kit/plugins"   # 客户放自定义取证/钩子脚本的地方
  if [ "$ENABLED" = "1" ]; then
    : > "${home}/.spire-kit/enabled"
  else
    rm -f "${home}/.spire-kit/enabled"
    log "以【关闭】状态安装(缺 enabled 标记):agent 与 shim 都不会启动,想开就 touch 该文件"
  fi
  install -m 0755 "${SELF_DIR}/spire-bootstrap.sh" "${home}/.spire-kit/bootstrap.sh"
  install -m 0644 "${SELF_DIR}/agent.conf.tmpl" "${home}/.spire-kit/agent.conf.tmpl"
  install -m 0644 "${SELF_DIR}/spire-agent.service" "${home}/.config/systemd/user/spire-agent.service"
  install -m 0755 "${SELF_DIR}/spire-header-shim.py" "${home}/.spire-kit/header-shim.py"
  install -m 0644 "${SELF_DIR}/shim.env.example" "${home}/.spire-kit/shim.env.example"
  install -m 0644 "${SELF_DIR}/spire-header-shim.service" "${home}/.config/systemd/user/spire-header-shim.service"
  # 开机自启:user manager 读 default.target.wants/。用相对 symlink,便于离线盘内自洽。
  ln -sfn "../spire-agent.service" "${home}/.config/systemd/user/default.target.wants/spire-agent.service"
  # shim 的自启软链也放上,但 unit 带 ConditionPathExists=%h/.spire-kit/shim.env —— 没配就不启动
  ln -sfn "../spire-header-shim.service" "${home}/.config/systemd/user/default.target.wants/spire-header-shim.service"

  cat > "${home}/.spire-kit/README" << 'GUESTREADME'
spire-kit(guest 侧)—— per-microVM SPIRE 身份,非侵入形态

开机流程:systemd --user → spire-agent.service → ~/.spire-kit/bootstrap.sh
  1. 找默认网关(= 本 VM /30 的 host 端)
  2. GET http://<gw>:8877/v1/join-token  取一次性 join token
  3. 渲染 ~/.spire-kit/agent.conf(server 坐标由 broker 回传)
  4. exec spire-agent run -joinToken <token>

Workload API socket:~/.spire-kit/run/agent.sock
OpenClaw(uid 1000)可直接连它取 JWT-SVID / X.509-SVID。

自检:
  systemctl --user status spire-agent
  journalctl --user -u spire-agent -n 50 --no-pager
  ~/.spire-kit/bin/spire-agent api fetch jwt -audience bgw \
      -socketPath ~/.spire-kit/run/agent.sock
GUESTREADME

  if [ -n "$AGENT_BIN" ]; then
    [ -f "$AGENT_BIN" ] || { echo "spire-agent 二进制不存在: $AGENT_BIN" >&2; exit 1; }
    install -m 0755 "$AGENT_BIN" "${home}/.spire-kit/bin/spire-agent"
    log "baked spire-agent binary ($(stat -c%s "$AGENT_BIN" 2>/dev/null || stat -f%z "$AGENT_BIN") bytes)"
  else
    log "WARN: 未提供 --agent-binary,kit 已装但没有 spire-agent 可执行文件 —— bootstrap 会 fail-closed 退出并记日志"
  fi

  # 归属:数据盘里 /home/agent 归 uid 1000。chown 失败(非 root)只告警,不中断。
  chown -R "${AGENT_UID}:${AGENT_GID}" "${home}/.spire-kit" "${home}/.config/systemd" 2>/dev/null \
    || log "WARN: chown 到 ${AGENT_UID}:${AGENT_GID} 失败(非 root?)—— 活体 guest 内以 ${AGENT_USER} 身份跑本脚本即可"

  log "installed into ${home}"
  for f in "${KIT_FILES[@]}"; do
    [ -e "${home}/${f}" ] && echo "  ok  ${home}/${f}" || echo "  MISSING ${home}/${f}"
  done
}

case "$MODE" in
  home)
    install_into_home "$TARGET"
    ;;
  template)
    # 模板盘的内容【就是】/home/agent 本身(launch-vm.sh 把它当 /dev/vdc 挂到 /home/agent)
    mount_and_install "$TARGET" install_into_home
    log "template patched: $TARGET(新租户开机即带 kit)"
    ;;
  rootfs)
    mount_and_install "$TARGET" install_into_rootfs
    log "rootfs patched: $TARGET(该镜像起的每台 VM 开机即带 kit)"
    ;;
  rootdir)
    install_into_rootfs "$TARGET"
    ;;
  *)
    usage 2
    ;;
esac
