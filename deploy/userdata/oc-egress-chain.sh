#!/usr/bin/env bash
set -Eeuo pipefail

# Experimental filter/FORWARD manager. The ordered rules come from the single
# shared spec in egress_sim.py; this file only translates that spec to iptables.
# It never modifies the nat table and never flushes an iptables built-in chain.

readonly LIVE_CHAIN="OPENCLAW-EGRESS"
readonly NEW_CHAIN="OPENCLAW-EGRESS-new"
readonly OLD_CHAIN="OPENCLAW-EGRESS-old"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SPEC_SCRIPT="${SCRIPT_DIR}/oc-egress-sim.py"
IPTABLES=(iptables -w 5)

VPC_CIDR="${VPC_CIDR:-}"
LITELLM_HOST="${LITELLM_HOST:-}"
LITELLM_PORT="${LITELLM_PORT:-4000}"
SPIRE_SERVER="${SPIRE_SERVER:-}"
TAP_IFACE="${TAP_IFACE:-tap+}"
DENY_RFC1918="${DENY_RFC1918:-false}"
# #566 follow-up — 运维额外放行洞(proto:dport:dst,逗号分隔),透传给 oc-egress-sim.py。
EGRESS_EXTRA_ALLOW="${EGRESS_EXTRA_ALLOW:-}"
export VPC_CIDR LITELLM_HOST LITELLM_PORT SPIRE_SERVER TAP_IFACE DENY_RFC1918 EGRESS_EXTRA_ALLOW


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
  if [[ -n "${LITELLM_HOST}" ]]; then
    "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" \
      -d "${LITELLM_HOST}" -p tcp --dport "${LITELLM_PORT}" -j ACCEPT
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
    -d "${VPC_CIDR}" -p tcp -j REJECT --reject-with tcp-reset
  "${IPTABLES[@]}" -C "${NEW_CHAIN}" -i "${TAP_IFACE}" -j RETURN
}


find_established_anchor() {
  local line
  local number=0
  while IFS= read -r line; do
    [[ "${line}" == "-A FORWARD "* ]] || continue
    number=$((number + 1))
    if [[ "${line}" == *"-m conntrack"* &&
          "${line}" == *"--ctstate "* &&
          "${line}" == *"RELATED"* &&
          "${line}" == *"ESTABLISHED"* &&
          "${line}" == *"-j ACCEPT"* ]]; then
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


apply_chain() {
  require_apply_config
  delete_chain "${NEW_CHAIN}"
  delete_chain "${OLD_CHAIN}"
  "${IPTABLES[@]}" -N "${NEW_CHAIN}"
  populate_scratch_chain
  verify_scratch_chain
  install_scratch_jump

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
