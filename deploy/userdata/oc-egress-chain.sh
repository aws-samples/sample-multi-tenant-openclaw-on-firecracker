#!/usr/bin/env bash
set -Eeuo pipefail

# Experimental filter/FORWARD manager. The ordered rules come from the single
# shared spec in egress_sim.py; this file only translates that spec to iptables.
# It never modifies the nat table and never flushes an iptables built-in chain.

# 链名前缀可覆写 —— 给探针/演练一套与在役链【完全隔离】的链。
#
# 为什么必须有这个开关:apply_chain 的 delete_forward_jumps "${LIVE_CHAIN}" 只按
# jump 的 target 匹配(见 forward_jump_number),【不看 -i】。所以任何人拿
# TAP_IFACE=<合成 tap> 跑一次 apply,都会顺带删掉保护全部真实租户的
# `-i tap+ ... -j OPENCLAW-EGRESS` 跳转,再把在役链改名+flush 掉 —— 只剩 per-tap
# DROP,共享白名单层消失,直到 host-agent 下一轮 15s reconcile 才补回来
# (agent 停了就是无限期)。合成 tap 只保证【新链的规则】不匹配真实 guest,
# 完全不保证【旧链】还在。
#
# 因此探针必须换一整套链名,而不是只换 TAP_IFACE。生产路径不设这个环境变量,
# 行为与之前逐字节相同。
CHAIN_BASE="${OC_EGRESS_CHAIN_BASE:-OPENCLAW-EGRESS}"
# 链名要拼进 iptables 参数,且 iptables 链名上限 28 字符(-new/-old 再占 4)。
if [[ ! "${CHAIN_BASE}" =~ ^[A-Za-z0-9_-]{1,24}$ ]]; then
  printf '[oc-egress] FATAL invalid OC_EGRESS_CHAIN_BASE=%s\n' "${CHAIN_BASE}" >&2
  exit 2
fi
readonly CHAIN_BASE
readonly LIVE_CHAIN="${CHAIN_BASE}"
readonly NEW_CHAIN="${CHAIN_BASE}-new"
readonly OLD_CHAIN="${CHAIN_BASE}-old"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SPEC_SCRIPT="${SCRIPT_DIR}/oc-egress-sim.py"
IPTABLES=(iptables -w 5)

VPC_CIDR="${VPC_CIDR:-}"
LITELLM_HOST="${LITELLM_HOST:-}"
LITELLM_CIDR="${LITELLM_CIDR:-}"
LITELLM_PORT="${LITELLM_PORT:-4000}"
SPIRE_SERVER="${SPIRE_SERVER:-}"
TAP_IFACE="${TAP_IFACE:-tap+}"
DENY_RFC1918="${DENY_RFC1918:-false}"
TENANT_SUPERNET="${TENANT_SUPERNET:-}"
# #566 follow-up — 运维额外放行洞(proto:dport:dst,逗号分隔),透传给 oc-egress-sim.py。
EGRESS_EXTRA_ALLOW="${EGRESS_EXTRA_ALLOW:-}"
export VPC_CIDR LITELLM_HOST LITELLM_CIDR LITELLM_PORT SPIRE_SERVER TAP_IFACE
export DENY_RFC1918 TENANT_SUPERNET EGRESS_EXTRA_ALLOW


log() {
  printf '[oc-egress] %s\n' "$*"
}


die() {
  printf '[oc-egress] ERROR: %s\n' "$*" >&2
  exit 1
}


require_platform() {
  [[ "$(uname -s)" == "Linux" ]] || die "Linux is required"
  [[ "${EUID}" -eq 0 ]] || die "root is required; rerun with sudo"
  command -v iptables >/dev/null 2>&1 || die "iptables is required"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
}


require_apply_config() {
  [[ -n "${VPC_CIDR}" ]] || die "VPC_CIDR is required and must not be empty"
  [[ -f "${SPEC_SCRIPT}" ]] || die "shared spec not found: ${SPEC_SCRIPT}"
}


chain_exists() {
  "${IPTABLES[@]}" -S "$1" >/dev/null 2>&1
}


forward_jump_number() {
  local target="$1"
  local line
  local number=0
  while IFS= read -r line; do
    [[ "${line}" == "-A FORWARD "* ]] || continue
    number=$((number + 1))
    if [[ "${line}" == *" -j ${target}" ]]; then
      printf '%s\n' "${number}"
      return 0
    fi
  done < <("${IPTABLES[@]}" -S FORWARD)
  return 1
}


delete_forward_jumps() {
  local target="$1"
  local number
  while number="$(forward_jump_number "${target}")"; do
    "${IPTABLES[@]}" -D FORWARD "${number}"
  done
}


delete_chain() {
  local chain="$1"
  delete_forward_jumps "${chain}"
  if chain_exists "${chain}"; then
    "${IPTABLES[@]}" -F "${chain}"
    "${IPTABLES[@]}" -X "${chain}"
  fi
}


append_reject_rule() {
  local chain="$1"
  local in_iface="$2"
  local destination="$3"
  "${IPTABLES[@]}" -A "${chain}" -i "${in_iface}" -d "${destination}" \
    -p tcp -j REJECT --reject-with tcp-reset
  # tcp-reset is invalid for non-TCP. This second physical rule completes the
  # logical all-protocol REJECT from the shared spec.
  "${IPTABLES[@]}" -A "${chain}" -i "${in_iface}" -d "${destination}" \
    -j REJECT --reject-with icmp-admin-prohibited
}


append_spec_rule() {
  local chain="$1"
  local action="$2"
  local in_iface="$3"
  local proto="$4"
  local dport="$5"
  local destination="$6"
  local -a args=(-A "${chain}" -i "${in_iface}")
  [[ -z "${destination}" ]] || args+=(-d "${destination}")
  [[ -z "${proto}" ]] || args+=(-p "${proto}")
  [[ -z "${dport}" ]] || args+=(--dport "${dport}")
  if [[ "${action}" == "REJECT" ]]; then
    append_reject_rule "${chain}" "${in_iface}" "${destination}"
    return
  fi
  case "${action}" in
    ACCEPT|DROP|RETURN) args+=(-j "${action}") ;;
    *) die "unsupported action from shared spec: ${action}" ;;
  esac
  "${IPTABLES[@]}" "${args[@]}"
}


populate_scratch_chain() {
  local rows
  local action in_iface proto dport destination note
  rows="$(python3 "${SPEC_SCRIPT}" --emit-rules)"
  while IFS='|' read -r action in_iface proto dport destination note; do
    [[ -n "${action}" ]] || continue
    log "append ${action}: ${note}"
    append_spec_rule \
      "${NEW_CHAIN}" "${action}" "${in_iface}" "${proto}" "${dport}" "${destination}"
  done <<<"${rows}"
}


verify_scratch_chain() {
  local cidr token proto dport destination rest
  local line number=0
  local imds_drop_index=0
  local dns_udp_index=0
  local dns_tcp_index=0
  local link_local_drop_index=0
  local first_llm_index=0
  local spire_accept_index=0
  local tenant_reject_index=0
  local first_extra_index=0
  local -a llm_destinations=()
  local -a extra_tokens=()
  if [[ -n "${LITELLM_CIDR}" ]]; then
    IFS=',' read -r -a llm_destinations <<<"${LITELLM_CIDR}"
    for cidr in "${llm_destinations[@]}"; do
      cidr="${cidr//[[:space:]]/}"
      [[ -n "${cidr}" ]] || continue
      "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
        -d "${cidr}" -p tcp --dport "${LITELLM_PORT}" -j ACCEPT
    done
  elif [[ -n "${LITELLM_HOST}" ]]; then
    llm_destinations=("${LITELLM_HOST}")
    "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
      -d "${LITELLM_HOST}" -p tcp --dport "${LITELLM_PORT}" -j ACCEPT
  fi
  if [[ -n "${TENANT_SUPERNET}" ]]; then
    "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
      -d "${TENANT_SUPERNET}" -p tcp -j REJECT --reject-with tcp-reset
  fi
  if [[ -n "${SPIRE_SERVER}" ]]; then
    "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
      -d "${SPIRE_SERVER}" -p tcp --dport 8081 -j ACCEPT
  fi
  "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
    -p udp --dport 53 -j ACCEPT
  "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
    -p tcp --dport 53 -j ACCEPT
  "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
    -d 169.254.169.254 -j DROP
  "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
    -d 169.254.0.0/16 -j DROP
  "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
    -d "${VPC_CIDR}" -p tcp -j REJECT --reject-with tcp-reset
  "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" -j RETURN

  if [[ -n "${EGRESS_EXTRA_ALLOW}" ]]; then
    IFS=',' read -r -a extra_tokens <<<"${EGRESS_EXTRA_ALLOW}"
  fi
  while IFS= read -r line; do
    [[ "${line}" == "-A ${NEW_CHAIN} "* ]] || continue
    number=$((number + 1))
    if [[ "${line}" == *" -d 169.254.169.254"* &&
          "${line}" == *" -j DROP"* ]]; then
      imds_drop_index="${number}"
    fi
    if [[ "${line}" == *" -p udp "* &&
          "${line}" == *" --dport 53 "* &&
          "${line}" == *" -j ACCEPT"* &&
          "${dns_udp_index}" -eq 0 ]]; then
      dns_udp_index="${number}"
    fi
    if [[ "${line}" == *" -p tcp "* &&
          "${line}" == *" --dport 53 "* &&
          "${line}" == *" -j ACCEPT"* &&
          "${dns_tcp_index}" -eq 0 ]]; then
      dns_tcp_index="${number}"
    fi
    if [[ "${line}" == *" -d 169.254.0.0/16"* &&
          "${line}" == *" -j DROP"* ]]; then
      link_local_drop_index="${number}"
    fi
    if [[ -n "${TENANT_SUPERNET}" &&
          "${line}" == *" -d ${TENANT_SUPERNET}"* &&
          "${line}" == *" -j REJECT"* &&
          "${tenant_reject_index}" -eq 0 ]]; then
      tenant_reject_index="${number}"
    fi
    for cidr in "${llm_destinations[@]}"; do
      cidr="${cidr//[[:space:]]/}"
      if [[ -n "${cidr}" &&
            "${line}" == *" -d ${cidr}"* &&
            "${line}" == *" -p tcp "* &&
            "${line}" == *" --dport ${LITELLM_PORT} "* &&
            "${line}" == *" -j ACCEPT"* &&
            "${first_llm_index}" -eq 0 ]]; then
        first_llm_index="${number}"
      fi
    done
    if [[ -n "${SPIRE_SERVER}" &&
          "${line}" == *" -d ${SPIRE_SERVER}"* &&
          "${line}" == *" -p tcp "* &&
          "${line}" == *" --dport 8081 "* &&
          "${line}" == *" -j ACCEPT"* &&
          "${spire_accept_index}" -eq 0 ]]; then
      spire_accept_index="${number}"
    fi
    for token in "${extra_tokens[@]}"; do
      token="${token//[[:space:]]/}"
      proto="${token%%:*}"
      rest="${token#*:}"
      dport="${rest%%:*}"
      destination="${rest#*:}"
      if [[ -n "${destination}" &&
            "${line}" == *" -d ${destination}"* &&
            "${line}" == *" -p ${proto} "* &&
            "${line}" == *" --dport ${dport} "* &&
            "${line}" == *" -j ACCEPT"* &&
            "${first_extra_index}" -eq 0 ]]; then
        first_extra_index="${number}"
      fi
    done
  done < <("${IPTABLES[@]}" -S "${NEW_CHAIN}")

  if [[ "${first_llm_index}" -gt 0 ]] &&
     [[ "${imds_drop_index}" -eq 0 || "${imds_drop_index}" -ge "${first_llm_index}" ]]; then
    die "scratch verification failed: IMDS DROP must precede LiteLLM ACCEPT"
  fi
  [[ "${link_local_drop_index}" -gt 0 ]] ||
    die "scratch verification failed: link-local DROP is missing"
  if [[ "${dns_udp_index}" -eq 0 || "${dns_tcp_index}" -eq 0 ||
        "${dns_udp_index}" -ge "${link_local_drop_index}" ||
        "${dns_tcp_index}" -ge "${link_local_drop_index}" ]]; then
    die "scratch verification failed: DNS ACCEPT must precede link-local DROP"
  fi
  if [[ "${first_llm_index}" -gt 0 &&
        "${link_local_drop_index}" -ge "${first_llm_index}" ]]; then
    die "scratch verification failed: link-local DROP must precede LiteLLM ACCEPT"
  fi
  if [[ "${spire_accept_index}" -gt 0 &&
        "${link_local_drop_index}" -ge "${spire_accept_index}" ]]; then
    die "scratch verification failed: link-local DROP must precede SPIRE ACCEPT"
  fi
  if [[ "${first_extra_index}" -gt 0 &&
        "${link_local_drop_index}" -ge "${first_extra_index}" ]]; then
    die "scratch verification failed: link-local DROP must precede extra_allow ACCEPT"
  fi
  if [[ -n "${TENANT_SUPERNET}" ]]; then
    [[ "${tenant_reject_index}" -gt 0 ]] ||
      die "scratch verification failed: tenant-supernet REJECT is missing"
    if [[ -n "${EGRESS_EXTRA_ALLOW}" ]]; then
      [[ "${first_extra_index}" -gt 0 ]] ||
        die "scratch verification failed: extra_allow ACCEPT is missing"
      [[ "${tenant_reject_index}" -lt "${first_extra_index}" ]] ||
        die "scratch verification failed: tenant REJECT must precede extra_allow ACCEPT"
    fi
  fi
}


find_established_anchor() {
  local line ctstates
  local number=0
  while IFS= read -r line; do
    [[ "${line}" == "-A FORWARD "* ]] || continue
    number=$((number + 1))
    ctstates=""
    if [[ "${line}" == *"--ctstate "* ]]; then
      ctstates="${line#*--ctstate }"
      ctstates="${ctstates%% *}"
    fi
    # 只能认领纯 RELATED,ESTABLISHED 锚点。含 NEW 或其它状态的 ACCEPT 会在
    # jump 之前放行 guest 新连接,绝不能拿它当插入位置。
    if [[ "${line}" == *"-m conntrack"* &&
          "${line}" == *"--ctstate "* &&
          ",${ctstates}," == *",RELATED,"* &&
          ",${ctstates}," == *",ESTABLISHED,"* &&
          ",${ctstates}," != *",NEW,"* &&
          ( "${ctstates}" == "RELATED,ESTABLISHED" ||
            "${ctstates}" == "ESTABLISHED,RELATED" ) &&
          "${line}" == *" -j ACCEPT" ]]; then
      printf '%s\n' "${number}"
      return 0
    fi
  done < <("${IPTABLES[@]}" -S FORWARD)
  return 1
}


install_scratch_jump() {
  local anchor
  delete_forward_jumps "${NEW_CHAIN}"
  if ! anchor="$(find_established_anchor)"; then
    # #566 M1 fix — a fresh host (no guest launched yet) has no conntrack
    # RELATED,ESTABLISHED ACCEPT anchor in FORWARD, because launch-vm.sh/migrate-vm.sh
    # create it per-guest. Previously we die'd here, so the config-path apply
    # (init-host at boot, before any guest) AND host-agent reconcile silently
    # fail-open. Self-create the canonical anchor at FORWARD top so the broad
    # VPC default-deny installs durably regardless of guest presence. Idempotent:
    # if launch-vm later inserts its own identical anchor, both are harmless ACCEPTs.
    log "no established-connection anchor in FORWARD; creating one (fresh-host path)"
    "${IPTABLES[@]}" -I FORWARD 1 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    anchor="$(find_established_anchor)" ||
      die "failed to create conntrack RELATED,ESTABLISHED ACCEPT anchor"
  fi
  "${IPTABLES[@]}" -C FORWARD -i "${TAP_IFACE}" \
    -m conntrack --ctstate NEW -j "${NEW_CHAIN}" 2>/dev/null ||
    "${IPTABLES[@]}" -I FORWARD "$((anchor + 1))" -i "${TAP_IFACE}" \
      -m conntrack --ctstate NEW -j "${NEW_CHAIN}"
}


# 装链之后必须验的第 4 道门:FORWARD 里【排在跳转之前】的规则不得放行 guest 流量。
#
# 由来(apse1 真机实测的版图,2026-08-25):
#   2..1181   per-tap DROP × 4/tap(6379 / 租户超网 / .253 / IMDS)   ← 锚点之前
#   1182      conntrack RELATED,ESTABLISHED ACCEPT(锚点)
#   1183      本脚本把跳转插在 anchor+1                              ← 正好落在这里
#   1184..    per-tap 兜底 "-i tapX -o enP1s33 -j ACCEPT" × 295      ← 锚点之后
#
# 这个相对位置是【载荷性不变量】而不是巧合:兜底 ACCEPT 一旦排到跳转之前,整条白名单链
# 就被短路 —— 链装着、规则对着、指纹也对,但一个包都不进去。三条既有收敛判据
# (presence / policy_version / 指纹漂移)全都看不见这种失效,因为它们只看链【内部】。
#
# 反方向也危险:若某条能改变控制流的规则排在锚点之前,它可能抢在 per-tap DROP 之后、
# 白名单之前把流量放掉。所以判据改成安全 target 白名单:DROP/REJECT/LOG、严格的
# RELATED,ESTABLISHED 锚点与目标链 jump 才安全;RETURN、自定义链、goto 与未知 target
# 一律 fail-closed。锚点本身是唯一 ACCEPT 豁免,且 ctstate 不能含 NEW。
verify_forward_precedence() {
  local target="$1"
  local line ctstates target_flag rule_target
  local number=0 jump=0
  local -a offenders=()
  while IFS= read -r line; do
    [[ "${line}" == "-A FORWARD "* ]] || continue
    number=$((number + 1))
    if [[ "${line}" == *" -j ${target}" ]]; then
      jump="${number}"
      break
    fi

    ctstates=""
    if [[ "${line}" == *"--ctstate "* ]]; then
      ctstates="${line#*--ctstate }"
      ctstates="${ctstates%% *}"
    fi
    target_flag=""
    rule_target=""
    if [[ "${line}" =~ [[:space:]](-j|-g)[[:space:]]+([^[:space:]]+) ]]; then
      target_flag="${BASH_REMATCH[1]}"
      rule_target="${BASH_REMATCH[2]}"
    fi

    # ACCEPT 只豁免严格的 RELATED,ESTABLISHED 锚点。显式排除 NEW,并拒绝
    # INVALID/UNTRACKED 等其它 ctstate,避免宽锚点短路所有 guest 新连接。
    if [[ "${target_flag}" == "-j" && "${rule_target}" == "ACCEPT" &&
          "${line}" == *"-m conntrack"* &&
          "${line}" == *"--ctstate "* &&
          ",${ctstates}," == *",RELATED,"* &&
          ",${ctstates}," == *",ESTABLISHED,"* &&
          ",${ctstates}," != *",NEW,"* &&
          ( "${ctstates}" == "RELATED,ESTABLISHED" ||
            "${ctstates}" == "ESTABLISHED,RELATED" ) ]]; then
      continue
    fi

    # 只关心可能命中 guest 入向的规则。安全 target 用白名单表达;无法解析 target
    # 也必须计 offender,不能因未知语法静默放过。
    if [[ "${line}" != *" -i "* || "${line}" == *" -i tap"* ]]; then
      if [[ "${target_flag}" == "-j" &&
            ( "${rule_target}" == "DROP" || "${rule_target}" == "REJECT" ||
              "${rule_target}" == "LOG" ) ]]; then
        continue
      fi
      offenders+=("${number}: ${line}")
    fi
  done < <("${IPTABLES[@]}" -S FORWARD)

  [[ "${jump}" -gt 0 ]] || die "precedence check: no FORWARD jump to ${target}"
  if [[ "${#offenders[@]}" -gt 0 ]]; then
    printf '[oc-egress] ERROR: %s\n' \
      "precedence check failed: these FORWARD rules may short-circuit guest traffic before the ${target} jump (pos ${jump}):" >&2
    printf '[oc-egress]   %s\n' "${offenders[@]}" >&2
    return 1
  fi
  log "precedence ok: no unsafe guest rule precedes the ${target} jump (pos ${jump})"
}


apply_chain() {
  require_apply_config
  delete_chain "${NEW_CHAIN}"
  delete_chain "${OLD_CHAIN}"
  "${IPTABLES[@]}" -N "${NEW_CHAIN}"
  populate_scratch_chain
  verify_scratch_chain
  install_scratch_jump
  # fail-closed:位置不对就把刚装的 scratch 跳转与链撤掉再退出,不留半成品,
  # 也不动仍在服务的 LIVE_CHAIN(它的 jump 要到下面才删)。
  if ! verify_forward_precedence "${NEW_CHAIN}"; then
    delete_chain "${NEW_CHAIN}"
    die "aborted apply: a guest-accepting FORWARD rule precedes the new chain jump"
  fi

  delete_forward_jumps "${LIVE_CHAIN}"
  if chain_exists "${LIVE_CHAIN}"; then
    "${IPTABLES[@]}" -E "${LIVE_CHAIN}" "${OLD_CHAIN}"
  fi
  "${IPTABLES[@]}" -E "${NEW_CHAIN}" "${LIVE_CHAIN}"
  delete_chain "${OLD_CHAIN}"
  log "installed ${LIVE_CHAIN} after the established-connection anchor"
}


teardown_chain() {
  delete_forward_jumps "${NEW_CHAIN}"
  delete_forward_jumps "${OLD_CHAIN}"
  delete_forward_jumps "${LIVE_CHAIN}"
  delete_chain "${NEW_CHAIN}"
  delete_chain "${OLD_CHAIN}"
  delete_chain "${LIVE_CHAIN}"
  log "removed experiment-owned jumps and chains"
}


show_chain() {
  "${IPTABLES[@]}" -S "${LIVE_CHAIN}"
}


usage() {
  printf 'Usage: %s {apply|teardown|show}\n' "$0" >&2
}


main() {
  require_platform
  case "${1:-}" in
    apply) apply_chain ;;
    teardown) teardown_chain ;;
    show) show_chain ;;
    *) usage; exit 2 ;;
  esac
}


main "$@"
