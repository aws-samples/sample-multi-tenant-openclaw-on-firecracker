#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# setup-egress-allowlist.sh — #39 microVM 出网默认拒绝白名单的 host 侧基建(dnsmasq + ipset)。
#
# 为什么独立成脚本(不内联进 init-host.sh):EC2 user-data 有 16KB 硬限,init-host.sh
# 经 base64+gzip 已逼近上限(memory: uswest2-deploy-deadlock-and-fixes)。把这段 ~3KB
# 逻辑挪到 S3 分发的独立脚本(同 host-agent.py / backup-data.sh 模式),init-host.sh 只留
# 一行下载+调用,不撑爆 user-data。setup.sh 上传到 S3、init-host.sh 拉到 host 后执行。
#
# 何时跑:init-host.sh 在 nginx 配置后调用一次(host 起时)。config
# security.egress_allowlist_enabled 默认 false → 本脚本直接跳过(host 零变化)。true →
# 起 host dnsmasq(透明 DNS 劫持的落点)+ 建共享 ipset(dnsmasq 边解析边灌 cognito-idp/
# s3/运营域名的真实 IP,launch-vm.sh 的 FORWARD -m set 据此放行)。逐个 tap 的
# DNAT/ACCEPT/DROP 规则在 launch-vm.sh 里做(每 VM 一份,-i $TAP 隔离)。
# 为什么基建放 host 全局:dnsmasq/ipset 是 host 级单例,不该每 VM 起一份。
#
# 变量来源:/etc/platform.env(init-host.sh 写,含 EGRESS_ALLOWLIST_ENABLED /
# EGRESS_ALLOWLIST_DOMAINS / EGRESS_DNS_UPSTREAM / OC_REGION)。全部幂等,可重跑。

set -eu
log() { echo "[oc:egress] $(date +%H:%M:%S) $*"; }

# platform.env 提供 EGRESS_* + OC_REGION;缺文件不致命(gate 视为关)。
[ -f /etc/platform.env ] && . /etc/platform.env 2>/dev/null || true
REGION="${OC_REGION:-${REGION:-ap-southeast-1}}"

if [ "${EGRESS_ALLOWLIST_ENABLED:-false}" != "true" ]; then
  log "egress allowlist disabled (default) — no dnsmasq/ipset, egress unchanged"
  exit 0
fi

log "egress allowlist ENABLED — installing dnsmasq + ipset (default-deny egress)"

# 取公网默认出口网卡(同 launch-vm.sh 的取法),用于 dnsmasq except-interface——绝不能
# fallback 到写死的 eth0:metal 上公网口常是 ens5/enp...,写错会导致 dnsmasq 没排除
# 真公网口 → 对外开 :53(安全问题)。取不到才退 eth0(兜底,配合 bind-dynamic)。
EGRESS_HOST_IFACE="$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)"
[ -z "${EGRESS_HOST_IFACE}" ] && EGRESS_HOST_IFACE="eth0"

# 装 ipset + dnsmasq(幂等)。Ubuntu/Debian: apt;失败不阻断(fail-safe,launch-vm 的
# -m set 规则在 ipset 缺失时会跳过,退回只放静态 CIDR + DNS)。
apt-get -o DPkg::Lock::Timeout=60 install -y -qq ipset dnsmasq > /dev/null 2>&1 || \
  log "WARN: ipset/dnsmasq install failed — egress FQDN allowlist will be degraded"

# 共享 ipset:hash:ip + timeout 让 CDN 短 TTL 的旧 IP 自动老化(防条目无限涨)。
# family inet = IPv4(guest 无 IPv6 栈,per-tap disable_ipv6=1)。-exist 幂等。
ipset create oc_egress_allow hash:ip family inet timeout 900 -exist 2>/dev/null || \
  log "WARN: ipset create oc_egress_allow failed"

# dnsmasq:监听 tap(bind-dynamic 自动绑 launch-vm 逐个后建的 tap),绝不在公网口起 53
# (except-interface),对内置 + 运营域名 ipset=/domain/oc_egress_allow(解析时灌 ipset,
# 子域同规则匹配 → s3.{region} 覆盖 virtual-hosted bucket 域名)。min-cache-ttl 拉长防抖动。
_EGRESS_DOMAIN_LINES=""
for _d in $(echo "${EGRESS_ALLOWLIST_DOMAINS:-}" | tr ',' ' '); do
  [ -n "${_d}" ] && _EGRESS_DOMAIN_LINES="${_EGRESS_DOMAIN_LINES}ipset=/${_d}/oc_egress_allow\n"
done
mkdir -p /etc/dnsmasq.d
{
  echo "# #39 出网 FQDN 白名单(setup-egress-allowlist.sh 生成,随重建继承)。改域名清单走"
  echo "# config security.egress_allowlist_domains + cdk deploy 重建,绝不手改运行态。"
  echo "port=53"
  echo "bind-dynamic"
  echo "except-interface=${EGRESS_HOST_IFACE}"
  echo "no-resolv"
  echo "server=${EGRESS_DNS_UPSTREAM:-8.8.8.8}"
  echo "min-cache-ttl=300"
  echo "ipset=/cognito-idp.${REGION}.amazonaws.com/oc_egress_allow"
  echo "ipset=/s3.${REGION}.amazonaws.com/oc_egress_allow"
  printf "%b" "${_EGRESS_DOMAIN_LINES}"
} > /etc/dnsmasq.d/oc-egress.conf

# Ubuntu 25 systemd-resolved 占 :53 → dnsmasq 起不来。dnsmasq 只需绑 tap IP:53
# (bind-dynamic + except public iface),与 resolved 绑 127.0.0.53 不冲突;若冲突
# 让 dnsmasq 只监听 tap 侧即可(bind-dynamic 已排除公网口)。enable+restart 幂等。
systemctl enable dnsmasq > /dev/null 2>&1 || true
systemctl restart dnsmasq 2>/dev/null || log "WARN: dnsmasq restart failed — check :53 conflict"
log "done: dnsmasq up (upstream=${EGRESS_DNS_UPSTREAM:-8.8.8.8}) ipset=oc_egress_allow domains=cognito-idp.${REGION}+s3.${REGION}+${EGRESS_ALLOWLIST_DOMAINS:-<none>}"
