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
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as error:
        raise ValueError(f"{name} must be an IPv4 or IPv6 CIDR") from error


def _parse_extra_allow(value: str) -> tuple:
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
            dst = str(_net)
        if dst is None:
            raise ValueError(
                f"EGRESS_EXTRA_ALLOW entry {token!r} must set a dst IP/CIDR "
                "(destination-unrestricted allow holes are refused)"
            )
        out.append((proto, dport, dst))
    return tuple(out)


def load_config(environment: Mapping[str, str]) -> EgressConfig:
    """Load and validate the rule inputs from an environment mapping."""
    tap_iface = environment.get("TAP_IFACE", "tap+")
    if not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,15}", tap_iface):
        raise ValueError("TAP_IFACE contains unsupported characters or is too long")
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
        extra_allow=_parse_extra_allow(environment.get("EGRESS_EXTRA_ALLOW", "")),
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
    rules: list[Rule] = []
    if config.litellm_host:
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
    for proto in ("udp", "tcp"):
        rules.append(_rule("ACCEPT", config, f"DNS over {proto.upper()}", proto, 53))
    rules.append(_rule("DROP", config, "Block IMDS", dst=IMDS_IP))
    # #566 follow-up — 运维经 API 加的额外放行洞。排在 IMDS DROP 之【后】(IMDS 永不可被
    # 重新放行)、VPC REJECT 之【前】(才能对内网目的地开洞)。每条限定 proto+dport+dst。
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
    return rules


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
