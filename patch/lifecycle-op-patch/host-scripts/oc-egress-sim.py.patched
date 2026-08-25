#!/usr/bin/env python3
"""Shared egress rule specification and first-match simulator."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from dataclasses import dataclass
from typing import Mapping, Optional


RFC1918_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
IMDS_IP = "169.254.169.254"
LINK_LOCAL_CIDR = "169.254.0.0/16"
INTERNAL_REDLINE_PORTS = (6379, 6380, 8877, 18789, 22, 8899, 9090, 9100)
DATASTORE_REDLINE_PORTS = (3306, 5432, 27017, 11211, 9200, 9300)
REDLINE_PORTS = INTERNAL_REDLINE_PORTS + DATASTORE_REDLINE_PORTS
# 放行洞的绝对最小前缀长度。与控制面 admission(egress_admin_service._ABSOLUTE_MIN_PREFIX)
# 同值同语义,tests/test_594_... 有同源断言。
#
# 为什么 host 侧也要这道门:控制面的 admission 只守 API 入口,而期望态落在 DDB 且
# host role 自己就有 openclaw-hosts 的读写权(deploy/stacks/compute.py:83)。直接改
# DDB 的 egress_extra_allow 可以绕过 API 侧的全部前缀校验。apse1 真机实测:
#   tcp:6379:<VPC>/17  → 被语义探针拦(红线端口,洞内取样命中)
#   tcp:8443:<VPC>/17  → 【放行】半个 VPC 的 8443 对所有 guest 敞开
#   tcp:8443:<VPC>/16  → 恰好被"探针无处取样"挡住,但那是副作用不是判据
# 也就是说非红线端口在 /17..../23 区间没有任何机械阻挡。host 侧是最终裁决者
# (kubelet 语义),这道门必须在这里,不能只在控制面。
_ABSOLUTE_MIN_PREFIX = 24
_MIN_PREFIX_ENV = "EGRESS_EXTRA_ALLOW_MIN_PREFIX"
VERDICTS = {"ACCEPT", "DROP", "REJECT", "PUBLIC_ACCEPT"}
Rule = dict[str, object]
Packet = dict[str, object]


@dataclass(frozen=True)
class EgressConfig:
    """Validated environment-derived configuration."""

    vpc_cidr: str
    litellm_host: Optional[str]
    litellm_port: int
    spire_server: Optional[str]
    tap_iface: str
    deny_rfc1918: bool
    # #566 follow-up — 运维经 API 加的额外放行洞 (proto, dport, dst)。dst 为【必填】的 IPv4
    # IP/CIDR(拒空 dst 与 IPv6:链是 IPv4-only,IPv6 会让 apply 失败)。用 tuple 保持 frozen 可哈希。
    extra_allow: tuple = ()
    # #575 Fix A — LiteLLM ALB/NLB 子网 CIDR+端口洞;tenant 超网在所有放行洞之前 REJECT。
    litellm_cidrs: tuple = ()
    tenant_supernet: Optional[str] = None


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return normalized == "true"


def _parse_port(value: str, name: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def _parse_ip(value: str, name: str) -> Optional[str]:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as error:
        raise ValueError(f"{name} must be an IP address") from error


def _parse_network(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} is required and must not be empty")
    value = value.strip()
    # 这两个值描述的是超网,不能让 ipaddress 把裸 IPv4 静默补成 /32:
    # 那会让内网 REJECT 只覆盖一个地址,而顺序/回读/收敛静态门仍然全部是绿的。
    if "/" not in value:
        raise ValueError(
            f"{name} must include an explicit CIDR prefix; without '/', a bare "
            "IPv4 address is interpreted as /32, so the internal REJECT covers "
            "only one address while all static gates remain green"
        )
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as error:
        raise ValueError(f"{name} must be an IPv4 or IPv6 CIDR") from error


def _min_allow_prefix(environment: Mapping[str, str]) -> int:
    """放行洞的最小前缀长度:env 只能收紧,绝不能放宽到绝对下限以下。"""
    raw = (environment.get(_MIN_PREFIX_ENV) or "").strip()
    prefix = _ABSOLUTE_MIN_PREFIX
    if raw:
        try:
            prefix = int(raw)
        except ValueError as error:
            raise ValueError(
                f"{_MIN_PREFIX_ENV} must be an integer, got {raw!r}"
            ) from error
    return max(prefix, _ABSOLUTE_MIN_PREFIX)


def _assert_hole_not_too_wide(network, min_prefix: int, name: str) -> None:
    if network.prefixlen < min_prefix:
        raise ValueError(
            f"{name} {network} is broader than the minimum allowed prefix "
            f"/{min_prefix}: a wide hole opens that port for every guest across "
            f"the range and no downstream gate narrows it back"
        )


def _parse_extra_allow(value: str, min_prefix: int = _ABSOLUTE_MIN_PREFIX) -> tuple:
    """Parse EGRESS_EXTRA_ALLOW='proto:dport:dst,proto:dport:dst' into ((proto,dport,dst),...).

    dst is REQUIRED and must be an IPv4 address or CIDR (empty dst and IPv6 are refused:
    the chain is IPv4-only). proto ∈ {tcp,udp}; dport 1-65535. Never allows IMDS
    (guarded: the IMDS DROP rule precedes these ACCEPT holes in build_rule_spec).
    """
    if not value or not value.strip():
        return ()
    out = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        parts = token.split(":")
        if len(parts) < 2:
            raise ValueError(f"EGRESS_EXTRA_ALLOW entry must be proto:dport[:dst], got {token!r}")
        proto = parts[0].strip().lower()
        if proto not in {"tcp", "udp"}:
            raise ValueError(f"EGRESS_EXTRA_ALLOW proto must be tcp|udp, got {proto!r}")
        dport = _parse_port(parts[1].strip(), "EGRESS_EXTRA_ALLOW dport")
        # 红线端口 denylist,与控制面 admission 同源(那边只守 API 入口;期望态落 DDB,
        # host role 有该表读写权 → 直改 DDB 可绕过。host 侧是最终裁决者)。
        # 语义探针门也会抓到大部分这类洞,但它靠"洞内取一址"取样,取样失败(洞覆盖整段)
        # 时报的是 probe address 而非"端口是红线",判据不该依赖那个副作用。
        if dport in REDLINE_PORTS:
            raise ValueError(
                f"EGRESS_EXTRA_ALLOW dport {dport} is an egress red-line port "
                "and must never be opened to guests"
            )
        dst_raw = ":".join(parts[2:]).strip() if len(parts) > 2 else ""
        dst = None
        if dst_raw:
            try:
                _net = ipaddress.ip_network(dst_raw, strict=False)
            except ValueError as error:
                raise ValueError(
                    f"EGRESS_EXTRA_ALLOW dst must be IP/CIDR, got {dst_raw!r}"
                ) from error
            # HIGH fix — 链是 IPv4-only iptables;IPv6 dst 会让 `iptables -d <v6>` apply 失败、
            # 整链换入中止(set -e),毒 token 又被 DDB 持久化 → reconcile 永久卡 / fresh-host
            # 静默 fail-open。IPv6 出网本就不被本 IPv4 链过滤,故这里直接拒 IPv6。
            if _net.version != 4:
                raise ValueError(
                    f"EGRESS_EXTRA_ALLOW dst must be IPv4 (chain is IPv4-only), got {dst_raw!r}"
                )
            _assert_hole_not_too_wide(_net, min_prefix, "EGRESS_EXTRA_ALLOW dst")
            dst = str(_net)
        if dst is None:
            raise ValueError(
                f"EGRESS_EXTRA_ALLOW entry {token!r} must set a dst IP/CIDR "
                "(destination-unrestricted allow holes are refused)"
            )
        out.append((proto, dport, dst))
    return tuple(out)


def _parse_optional_ipv4_network(value: str, name: str) -> Optional[str]:
    if not value or not value.strip():
        return None
    network = ipaddress.ip_network(_parse_network(value, name), strict=False)
    if network.version != 4:
        raise ValueError(f"{name} must be IPv4 (chain is IPv4-only)")
    return str(network)


def _parse_litellm_cidrs(
    value: str,
    tenant_supernet: Optional[str],
    min_prefix: int = _ABSOLUTE_MIN_PREFIX,
) -> tuple:
    """Parse and fail closed on unsafe LiteLLM IPv4 CIDR allow holes.

    前缀下限与 extra_allow 对称。缺这条时最危险的一格是:把它配成客户整个 VPC 的
    /16 且 LITELLM_PORT=80 —— 行位断言过(洞在 IMDS/link-local/租户 REJECT 之后)、
    语义探针过(IMDS 不在 VPC 段内、与租户超网不重叠、:80 不在红线端口集)、scratch
    回读过、真机红线探针也全过,于是整个 VPC 的 :80 对所有 guest 敞开而每一道门都是绿的。
    客户生产的 LLM 网关恰好在 :80,且客户会被建议「放行覆盖它们的内网子网」,
    这正是最容易被配宽的一步。多 AZ 的 ALB 子网各配一条 /24 即可,不需要 /16。
    """
    if not value or not value.strip():
        return ()
    tenant = (
        ipaddress.ip_network(tenant_supernet, strict=False)
        if tenant_supernet
        else None
    )
    networks = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError as error:
            raise ValueError(
                f"LITELLM_CIDR entry must be an IPv4 CIDR, got {token!r}"
            ) from error
        if network.version != 4:
            raise ValueError(
                f"LITELLM_CIDR entry must be IPv4 (chain is IPv4-only), got {token!r}"
            )
        if network == ipaddress.ip_network("0.0.0.0/0"):
            raise ValueError("LITELLM_CIDR must not contain 0.0.0.0/0")
        if ipaddress.ip_address(IMDS_IP) in network:
            raise ValueError(f"LITELLM_CIDR must not contain IMDS ({IMDS_IP})")
        if tenant and network.overlaps(tenant):
            raise ValueError(
                f"LITELLM_CIDR {network} must not overlap TENANT_SUPERNET {tenant}"
            )
        _assert_hole_not_too_wide(network, min_prefix, "LITELLM_CIDR entry")
        networks.append(str(network))
    return tuple(networks)


def load_config(environment: Mapping[str, str]) -> EgressConfig:
    """Load and validate the rule inputs from an environment mapping."""
    tap_iface = environment.get("TAP_IFACE", "tap+")
    if not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,15}", tap_iface):
        raise ValueError("TAP_IFACE contains unsupported characters or is too long")
    tenant_supernet = _parse_optional_ipv4_network(
        environment.get("TENANT_SUPERNET", ""), "TENANT_SUPERNET"
    )
    min_prefix = _min_allow_prefix(environment)
    return EgressConfig(
        vpc_cidr=_parse_network(environment.get("VPC_CIDR", ""), "VPC_CIDR"),
        litellm_host=_parse_ip(
            environment.get("LITELLM_HOST", ""), "LITELLM_HOST"
        ),
        litellm_port=_parse_port(
            environment.get("LITELLM_PORT", "4000"), "LITELLM_PORT"
        ),
        spire_server=_parse_ip(
            environment.get("SPIRE_SERVER", ""), "SPIRE_SERVER"
        ),
        tap_iface=tap_iface,
        deny_rfc1918=_parse_bool(
            environment.get("DENY_RFC1918", "false"), "DENY_RFC1918"
        ),
        extra_allow=_parse_extra_allow(
            environment.get("EGRESS_EXTRA_ALLOW", ""), min_prefix
        ),
        litellm_cidrs=_parse_litellm_cidrs(
            environment.get("LITELLM_CIDR", ""), tenant_supernet, min_prefix
        ),
        tenant_supernet=tenant_supernet,
    )


def _rule(
    action: str,
    config: EgressConfig,
    note: str,
    proto: Optional[str] = None,
    dport: Optional[int] = None,
    dst: Optional[str] = None,
) -> Rule:
    return {
        "action": action,
        "in_iface": config.tap_iface,
        "proto": proto,
        "dport": dport,
        "dst": dst,
        "note": note,
    }


def build_rule_spec(config: EgressConfig) -> list[Rule]:
    """Return the single ordered rule specification used by all entry points."""
    rules: list[Rule] = [_rule("DROP", config, "Block IMDS", dst=IMDS_IP)]
    if config.tenant_supernet:
        rules.append(
            _rule(
                "REJECT",
                config,
                "Block tenant supernet",
                dst=config.tenant_supernet,
            )
        )
    for proto in ("udp", "tcp"):
        rules.append(_rule("ACCEPT", config, f"DNS over {proto.upper()}", proto, 53))
    # DNS 兜底必须先于本规则;link-local DROP 又必须先于所有 allow 洞,避免 extra_allow
    # 在 IMDS 同段重新开洞。
    rules.append(
        _rule("DROP", config, "Block link-local", dst=LINK_LOCAL_CIDR)
    )
    if config.litellm_cidrs:
        for cidr in config.litellm_cidrs:
            rules.append(
                _rule(
                    "ACCEPT",
                    config,
                    "LiteLLM allow hole",
                    "tcp",
                    config.litellm_port,
                    cidr,
                )
            )
    elif config.litellm_host:
        rules.append(
            _rule(
                "ACCEPT",
                config,
                "LiteLLM allow hole",
                "tcp",
                config.litellm_port,
                config.litellm_host,
            )
        )
    if config.spire_server:
        rules.append(
            _rule(
                "ACCEPT", config, "SPIRE allow hole", "tcp", 8081, config.spire_server
            )
        )
    # #566 follow-up — 运维经 API 加的额外放行洞。排在 IMDS DROP 之【后】(IMDS 永不可被
    # 重新放行)、VPC REJECT 之【前】(才能对内网目的地开洞)。每条限定 proto+dport+dst。
    # #575 Fix A — tenant REJECT 也排在这些洞之前,使 extra_allow 不能越过跨租户红线。
    for proto, dport, dst in config.extra_allow:
        rules.append(
            _rule("ACCEPT", config, f"extra allow hole {proto}:{dport}:{dst}", proto, dport, dst)
        )
    denied_networks = [config.vpc_cidr]
    if config.deny_rfc1918:
        denied_networks.extend(RFC1918_CIDRS)
    rules.extend(
        _rule("REJECT", config, "Internal default-deny", dst=network)
        for network in denied_networks
    )
    rules.append(
        _rule("RETURN", config, "Defer public traffic to existing FORWARD policy")
    )
    _assert_fail_closed_order(rules, config)
    _assert_no_redline_reachable(rules, config)
    return rules


def _assert_fail_closed_order(rules: list[Rule], config: EgressConfig) -> None:
    """Abort chain emission if an isolation rule moved below an allow hole."""
    try:
        imds_index = next(
            index
            for index, rule in enumerate(rules)
            if rule["action"] == "DROP" and rule["dst"] == IMDS_IP
        )
    except StopIteration as error:
        raise ValueError("fail-closed order check: IMDS DROP is missing") from error
    llm_indices = [
        index
        for index, rule in enumerate(rules)
        if rule["action"] == "ACCEPT" and rule["note"] == "LiteLLM allow hole"
    ]
    if any(imds_index >= index for index in llm_indices):
        raise ValueError("fail-closed order check: IMDS DROP must precede LiteLLM ACCEPT")
    try:
        link_local_index = next(
            index
            for index, rule in enumerate(rules)
            if rule["action"] == "DROP" and rule["dst"] == LINK_LOCAL_CIDR
        )
    except StopIteration as error:
        raise ValueError(
            "fail-closed order check: link-local DROP is missing"
        ) from error
    allow_hole_indices = [
        index
        for index, rule in enumerate(rules)
        if rule["action"] == "ACCEPT"
        and (
            rule["note"] in {"LiteLLM allow hole", "SPIRE allow hole"}
            or str(rule["note"]).startswith("extra allow hole ")
        )
    ]
    if allow_hole_indices and link_local_index >= min(allow_hole_indices):
        raise ValueError(
            "fail-closed order check: link-local DROP must precede "
            "LiteLLM/SPIRE/extra ACCEPT"
        )
    if not config.tenant_supernet:
        return
    try:
        tenant_index = next(
            index
            for index, rule in enumerate(rules)
            if rule["action"] == "REJECT" and rule["dst"] == config.tenant_supernet
        )
    except StopIteration as error:
        raise ValueError("fail-closed order check: tenant REJECT is missing") from error
    protected_accepts = [
        index
        for index, rule in enumerate(rules)
        if rule["action"] == "ACCEPT"
        and (
            rule["note"] == "LiteLLM allow hole"
            or str(rule["note"]).startswith("extra allow hole ")
        )
    ]
    if protected_accepts and tenant_index >= min(protected_accepts):
        raise ValueError(
            "fail-closed order check: tenant REJECT must precede LiteLLM/extra ACCEPT"
        )


def _interface_matches(pattern: str, interface: str) -> bool:
    if pattern.endswith("+"):
        return interface.startswith(pattern[:-1])
    return pattern == interface


def _destination_matches(destination: object, packet_ip: ipaddress._BaseAddress) -> bool:
    if destination is None:
        return True
    network = ipaddress.ip_network(str(destination), strict=False)
    return packet_ip.version == network.version and packet_ip in network


def _rule_matches(rule: Rule, packet: Packet, packet_ip: ipaddress._BaseAddress) -> bool:
    if not _interface_matches(str(rule["in_iface"]), str(packet["in_iface"])):
        return False
    if rule["proto"] is not None and rule["proto"] != packet["proto"]:
        return False
    if rule["dport"] is not None and rule["dport"] != packet["dport"]:
        return False
    return _destination_matches(rule["dst"], packet_ip)


def evaluate_packet(packet: Packet, rules: list[Rule]) -> str:
    """Evaluate one packet using conntrack bypass and ordered first-match rules."""
    ctstate = str(packet["ctstate"]).upper()
    if ctstate in {"ESTABLISHED", "RELATED"}:
        return "ACCEPT"
    packet_ip = ipaddress.ip_address(str(packet["dst_ip"]))
    for rule in rules:
        if not _rule_matches(rule, packet, packet_ip):
            continue
        action = str(rule["action"])
        return "PUBLIC_ACCEPT" if action == "RETURN" else action
    return "PUBLIC_ACCEPT"


def _probe_interface(config: EgressConfig) -> str:
    return (
        f"{config.tap_iface[:-1]}0"
        if config.tap_iface.endswith("+")
        else config.tap_iface
    )


def _redline_packet(
    config: EgressConfig, dst_ip: str, dport: int, proto: str
) -> Packet:
    return {
        "in_iface": _probe_interface(config),
        "dst_ip": dst_ip,
        "dport": dport,
        "proto": proto,
        "ctstate": "NEW",
    }


def _first_usable_address(network: ipaddress._BaseNetwork, name: str) -> str:
    try:
        return str(next(network.hosts()))
    except StopIteration as error:
        raise ValueError(
            f"redline reachable: {name} has no usable probe address"
        ) from error


def _vpc_probe_address(rules: list[Rule], config: EgressConfig) -> str:
    vpc = ipaddress.ip_network(config.vpc_cidr, strict=False)
    remaining = [vpc]
    allow_destinations = [
        ipaddress.ip_network(str(rule["dst"]), strict=False)
        for rule in rules
        if rule["action"] == "ACCEPT" and rule["dst"] is not None
    ]
    for allowed in allow_destinations:
        if allowed.version != vpc.version or not allowed.overlaps(vpc):
            continue
        next_remaining = []
        for candidate in remaining:
            if not candidate.overlaps(allowed):
                next_remaining.append(candidate)
            elif allowed == candidate or allowed.supernet_of(candidate):
                continue
            elif candidate.supernet_of(allowed):
                next_remaining.extend(candidate.address_exclude(allowed))
        remaining = next_remaining
        if not remaining:
            break
    if not remaining:
        # 说清真因:走到这里意味着 allow 洞把整个 VPC_CIDR 覆盖满了,于是没有任何
        # VPC 内地址能用来验证「洞外的红线端口不可达」。原文案只报 probe address,
        # 运维读不出「你的洞开太宽」。前缀下限门通常先拦掉这类洞,这里是兜底。
        raise ValueError(
            "redline reachable: allow holes cover the entire VPC_CIDR "
            f"({config.vpc_cidr}), so no in-VPC address is left to prove the "
            "red-line ports stay unreachable — narrow the allow holes"
        )
    probe_network = min(
        remaining, key=lambda network: (int(network.network_address), network.prefixlen)
    )
    return _first_usable_address(probe_network, "VPC_CIDR")


def _assert_no_redline_reachable(
    rules: list[Rule], config: EgressConfig
) -> None:
    """Fail closed when this module's evaluator can reach a red-line packet."""
    probes = []
    for dport in (80, 443, 53):
        for proto in ("tcp", "udp"):
            probes.append(
                (
                    f"IMDS {proto}/{dport}",
                    _redline_packet(config, IMDS_IP, dport, proto),
                )
            )
    for dport in (80, 22):
        probes.append(
            (
                f"link-local tcp/{dport}",
                _redline_packet(config, "169.254.0.1", dport, "tcp"),
            )
        )
    if config.tenant_supernet:
        tenant = ipaddress.ip_network(config.tenant_supernet, strict=False)
        probes.append(
            (
                "tenant supernet tcp/18789",
                _redline_packet(
                    config,
                    _first_usable_address(tenant, "TENANT_SUPERNET"),
                    18789,
                    "tcp",
                ),
            )
        )
    vpc_probe = _vpc_probe_address(rules, config)
    for dport in REDLINE_PORTS:
        probes.append(
            (
                f"VPC red-line tcp/{dport}",
                _redline_packet(config, vpc_probe, dport, "tcp"),
            )
        )
    # 洞【内】也必须探。只探"洞外地址"的探针集会被一条把红线端口开在自己网段内的洞绕过:
    # 真机实测(坐标见 engineering/evidence/,不落进本文件):把一个 /24 的洞开在 valkey
    # 所在网段时,洞外探针被选在该 /24 之外 → 6379 判 REJECT → 门放行 → 而 guest 对洞内
    # 那台 valkey 的 6379 实际拿到了 +PONG。
    # 因此对每个 allow 洞的 dst 网段各取一个地址,再跑一遍红线端口。
    # 只探与内网范围(VPC / 租户超网 / link-local)有交集的洞:红线的语义是"guest 不得触达
    # 内网基础设施",纯公网洞的红线端口会命中链末 RETURN 得 PUBLIC_ACCEPT,那正是"公网默认
    # 放行"的设计,不是缺口 —— 不加这个限定会把公网 LLM 网关的洞误报成红线可达。
    # 洞限定了 dport 的情况(LiteLLM 4000 / SPIRE 8081)不会误报:那些 dport 不在 REDLINE_PORTS。
    internal_ranges = [ipaddress.ip_network(config.vpc_cidr, strict=False),
                       ipaddress.ip_network(LINK_LOCAL_CIDR)]
    if config.tenant_supernet:
        internal_ranges.append(
            ipaddress.ip_network(config.tenant_supernet, strict=False)
        )
    if config.deny_rfc1918:
        internal_ranges.extend(
            ipaddress.ip_network(cidr) for cidr in RFC1918_CIDRS
        )
    for rule in rules:
        if rule["action"] != "ACCEPT" or rule["dst"] is None:
            continue
        try:
            hole = ipaddress.ip_network(str(rule["dst"]), strict=False)
        except ValueError:
            continue
        if not any(hole.overlaps(internal) for internal in internal_ranges):
            continue
        hole_probe = (
            str(hole.network_address)
            if hole.prefixlen >= 31
            else _first_usable_address(hole, str(rule["dst"]))
        )
        for dport in REDLINE_PORTS:
            probes.append(
                (
                    f"inside allow hole {rule['dst']} red-line tcp/{dport}",
                    _redline_packet(config, hole_probe, dport, "tcp"),
                )
            )
    for name, packet in probes:
        verdict = evaluate_packet(packet, rules)
        if verdict in {"ACCEPT", "PUBLIC_ACCEPT"}:
            raise ValueError(f"redline reachable: {name} -> {verdict}")


def _fixed_selftest_config() -> EgressConfig:
    return load_config(
        {
            "VPC_CIDR": "10.20.0.0/16",
            "LITELLM_HOST": "10.20.1.10",
            "LITELLM_PORT": "4000",
            "SPIRE_SERVER": "10.20.1.20",
            "TAP_IFACE": "tap+",
            "DENY_RFC1918": "false",
        }
    )


def _fixed_cidr_selftest_config() -> EgressConfig:
    return load_config(
        {
            "VPC_CIDR": "10.20.0.0/16",
            "LITELLM_CIDR": "10.20.1.0/24,10.20.2.0/24",
            "LITELLM_PORT": "80",
            "TENANT_SUPERNET": "172.16.0.0/16",
            "TAP_IFACE": "tap+",
            "DENY_RFC1918": "false",
        }
    )


def _packet(dst_ip: str, dport: int, proto: str, ctstate: str = "NEW") -> Packet:
    return {
        "in_iface": "tap0",
        "dst_ip": dst_ip,
        "dport": dport,
        "proto": proto,
        "ctstate": ctstate,
    }


def _selftest_cases() -> list[tuple[str, Packet, str]]:
    return [
        ("VPC Redis is rejected", _packet("10.20.5.5", 6379, "tcp"), "REJECT"),
        ("LiteLLM is allowed", _packet("10.20.1.10", 4000, "tcp"), "ACCEPT"),
        ("Public DNS is allowed", _packet("8.8.8.8", 53, "udp"), "ACCEPT"),
        ("Public HTTPS returns", _packet("1.1.1.1", 443, "tcp"), "PUBLIC_ACCEPT"),
        ("IMDS is dropped", _packet(IMDS_IP, 80, "tcp"), "DROP"),
        (
            "Established VPC flow bypasses chain",
            _packet("10.20.5.5", 6379, "tcp", "ESTABLISHED"),
            "ACCEPT",
        ),
        ("SPIRE is allowed", _packet("10.20.1.20", 8081, "tcp"), "ACCEPT"),
        *[
            (
                f"Red-line port {dport} is rejected",
                _packet("10.20.9.9", dport, "tcp"),
                "REJECT",
            )
            for dport in REDLINE_PORTS
        ],
        # 这条只断言【共享链单层】的判决,不代表内核。真机上 per-tap 层
        # (launch-vm.sh:2264-2265)对 .253 是【全端口】DROP 且 -I FORWARD 1 插在
        # (launch-vm.sh:2338-2342)。两种情况下 .253:53 都不可达 —— 2026-08-25 在
        # 一台承载 192 租户的生产 host 上只读实测 137/137 个活 tap 全有该 DROP。
        # 详见 ADR-egress-allow-hole-redline.md §5.1。保留该洞是无害的(放行一条
        # 谁也走不到的路径),但不要据此以为 guest 靠它做 DNS。
        (
            "VPC DNS stub fallback stays allowed",
            _packet("169.254.169.253", 53, "udp"),
            "ACCEPT",
        ),
        (
            "Other link-local HTTP is dropped",
            _packet("169.254.0.123", 80, "tcp"),
            "DROP",
        ),
    ]


def _cidr_selftest_cases() -> list[tuple[str, Packet, str]]:
    return [
        ("LiteLLM CIDR A is allowed", _packet("10.20.1.10", 80, "tcp"), "ACCEPT"),
        ("LiteLLM CIDR B is allowed", _packet("10.20.2.10", 80, "tcp"), "ACCEPT"),
        ("IMDS :80 stays dropped", _packet(IMDS_IP, 80, "tcp"), "DROP"),
        ("Tenant supernet is rejected", _packet("172.16.5.5", 80, "tcp"), "REJECT"),
        ("Non-LLM VPC :80 is rejected", _packet("10.20.5.5", 80, "tcp"), "REJECT"),
        *[
            (
                f"CIDR red-line port {dport} is rejected",
                _packet("10.20.9.9", dport, "tcp"),
                "REJECT",
            )
            for dport in REDLINE_PORTS
        ],
        (
            "CIDR VPC DNS stub fallback stays allowed",
            _packet("169.254.169.253", 53, "tcp"),
            "ACCEPT",
        ),
        (
            "CIDR other link-local HTTP is dropped",
            _packet("169.254.0.123", 80, "tcp"),
            "DROP",
        ),
    ]


def run_selftest() -> int:
    """Run fixed examples plus a negative control and print a result table."""
    config = _fixed_selftest_config()
    rules = build_rule_spec(config)
    results = [
        (name, expected, evaluate_packet(packet, rules))
        for name, packet, expected in _selftest_cases()
    ]
    without_vpc_reject = [
        rule
        for rule in rules
        if not (rule["action"] == "REJECT" and rule["dst"] == config.vpc_cidr)
    ]
    negative_actual = evaluate_packet(
        _packet("10.20.5.5", 6379, "tcp"), without_vpc_reject
    )
    results.append(
        ("Negative control without VPC REJECT", "PUBLIC_ACCEPT", negative_actual)
    )
    cidr_rules = build_rule_spec(_fixed_cidr_selftest_config())
    results.extend(
        (name, expected, evaluate_packet(packet, cidr_rules))
        for name, packet, expected in _cidr_selftest_cases()
    )
    try:
        load_config(
            {
                "VPC_CIDR": "10.20.0.0/16",
                "LITELLM_CIDR": "172.16.1.0/24",
                "TENANT_SUPERNET": "172.16.0.0/16",
            }
        )
        overlap_actual = "ACCEPTED"
    except ValueError:
        overlap_actual = "REJECTED"
    results.append(
        ("Overlapping LiteLLM CIDR is rejected", "REJECTED", overlap_actual)
    )
    print(f"{'STATUS':<7} {'CASE':<42} {'EXPECTED':<14} ACTUAL")
    failures = 0
    for name, expected, actual in results:
        status = "PASS" if actual == expected else "FAIL"
        failures += status == "FAIL"
        print(f"{status:<7} {name:<42} {expected:<14} {actual}")
    return 1 if failures else 0


def emit_rules() -> int:
    """Emit delimiter-safe rows consumed by oc-egress-chain.sh."""
    for rule in build_rule_spec(load_config(os.environ)):
        fields = (
            rule["action"],
            rule["in_iface"],
            rule["proto"],
            rule["dport"],
            rule["dst"],
            rule["note"],
        )
        print("|".join("" if value is None else str(value) for value in fields))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selftest", action="store_true", help="run logic checks")
    mode.add_argument(
        "--emit-rules",
        action="store_true",
        help="emit the shared rule spec for the Linux apply script",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return run_selftest() if args.selftest else emit_rules()
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
