# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/egress_admin_service — 控制面 fleet guest 出网防火墙运维 API(ADR 拆分项②)。

实现 `POST /hosts/egress`:运维者一次调用即修改全部(或指定)host 的 guest 出网
default-deny 白名单链 `OPENCLAW-EGRESS`,拿到 API 响应 + 逐机一致性证据;
`GET /hosts/egress` 只读聚合 host-agent 上报的 fleet 收敛状态。

设计照抄 fleet_power(#…)的「一条 SSM send_command 扇出全机队」+ host taint(#539/#540)
的「声明式期望态写 DDB openclaw-hosts」——纯 SSM push 在 host 重建/重启后丢失,故期望态
必须落 DDB 让 host-agent 开机/poll reconcile(见 host-agent.py egress reconcile)。

铁律护栏(与 ADR guest-egress-default-deny-whitelist / issue host-firewall-admin-api 一致):
  · mode 只接受 off|deny(白名单校验,防注入)。
  · 下发脚本只跑 oc-egress-chain.sh apply|teardown —— 该脚本永不触碰 nat 表 / 租户 DNAT
    :18789 / MASQUERADE(由脚本自身 + 对抗测试保证);本 service 不接受任意 iptables 输入。
  · 链只 -i tap+(guest 来源),锚在 conntrack RELATED,ESTABLISHED ACCEPT 之后 + --ctstate
    NEW,不误伤租户数据面回包。

依赖方向:services → core(clients/utils/auth/audit)。
"""

import base64
import json
import os
import re
import time

import core.ddb_scan as ddb_scan
import core.clients as clients
from core.utils import _resp, _now
import core.auth as auth

VALID_MODES = ("off", "deny")
_REGION = os.environ.get("OC_REGION") or os.environ.get("AWS_REGION", "ap-southeast-1")
# #566 follow-up — 全机队扇出用 tag Targets(而非 InstanceIds):InstanceIds 上限 50、且
# 遇到列表里有正在终止的实例会被 SSM 整批拒(InvalidInstanceId);tag 匹配无 50 上限、
# 自动跳过非法态实例、且含未来新机。metal-host 的 Role tag 值。
HOST_TAG_ROLE = os.environ.get("HOST_TAG_ROLE", "metal-host")
_IMDS = "169.254.169.254"
# 本仓已知内部面:valkey/redis 6379/6380(launch-vm.sh:2293)、SPIRE join-broker
# 8877(spire-kit/install.sh:17)、tenant gateway 18789(route_ops.py:35)、host 管理面
# 22/8899/9090/9100(launch-vm.sh:2311)。
_INTERNAL_REDLINE_PORTS = frozenset(
    {22, 6379, 6380, 8877, 8899, 9090, 9100, 18789}
)
# 通用数据存储端口,作为纵深防御。
_DATASTORE_REDLINE_PORTS = frozenset(
    {3306, 5432, 9200, 9300, 11211, 27017}
)
_EXTRA_ALLOW_REDLINE_PORTS = _INTERNAL_REDLINE_PORTS | _DATASTORE_REDLINE_PORTS


def _is_dispatchable_host_id(instance_id):
    """是否是真正可下发的 EC2 host id。

    `__` 前缀是表内内部哨兵行;`i-` 前缀是 EC2 instance id 的基本形状。两条都要:
    只排哨兵会让来源不明的脏 id 进入 SSM InstanceIds,从而使同批合法 host 整批下发失败。
    """
    iid = str(instance_id or "")
    return not iid.startswith("__") and iid.startswith("i-")


def _summarize_rules_sha256(values):
    """只按真实上报的指纹区分一致、分歧与无人上报三种事实。"""
    reported = set()
    for value in values:
        sha = str(value or "").strip()
        if not sha or sha == "(not reported)":
            continue
        reported.add(sha)
    if not reported:
        return None, "no host reported a fingerprint"
    if len(reported) > 1:
        return None, "hosts disagree"
    return next(iter(reported)), "reported hosts agree"


# 放行洞的作用域下界分两个数,别把它们看成一个:
#
# 为什么必须有一个不依赖环境坐标的下界 —— 真机读数(us-west-2 `openclaw-api`,82 项 env)
# 里【没有】 VPC_CIDR / EGRESS_VPC_CIDR,只有 VPC_ID 与 VM_SUBNET_PREFIX。VPC 网段在控制面
# 不可判定,所以「dst 不得覆盖 VPC_CIDR」这条只能在注入了该 env 的环境生效。若把唯一的
# 下界建在它上面,缺该 env 的环境里作用域就完全没有机械阻挡。
#
# env 未设时的默认值。#668 放宽 admission 只降【绝对下界】,这个默认值不动 ——
# 既有调用方的行为逐字节不变,放宽必须由运维显式设 env,不是静默生效。
_DEFAULT_MIN_PREFIX = 24
# env 的取值下界:EGRESS_EXTRA_ALLOW_MIN_PREFIX 只能落在 [_ABSOLUTE_MIN_PREFIX, 32]。
# #668 把它从 24 降到 16 —— /16 以内的整网段洞由调用方自己承担风险(ADR 第 12 节)。
# 为什么保留一个下界而不是取消:/8 这种量级的洞会吞掉 host 侧语义探针的全部取样空间,
# 让「红线端口 + 探针」这套护栏一起退化,而比 /16 更宽的洞没有任何已知运维场景。
# 与 host 侧 oc-egress-sim.py::_ABSOLUTE_MIN_PREFIX_EXTRA_ALLOW 同值同源
# (test_594 有同源断言);那边平台配置 LiteLLM CIDR 仍守各自的 /24。
_ABSOLUTE_MIN_PREFIX = 16
# dry-run 一次最多判多少条:有界操作,免得一次 POST 一万条把 Lambda 时间耗在纯计算上。
_VALIDATE_MAX_ENTRIES = 64


def _extra_allow_min_prefix():
    raw = os.environ.get(
        "EGRESS_EXTRA_ALLOW_MIN_PREFIX", str(_DEFAULT_MIN_PREFIX)
    ).strip()
    try:
        prefix = int(raw)
    except ValueError as error:
        raise ValueError(
            "EGRESS_EXTRA_ALLOW_MIN_PREFIX must be an integer between 0 and 32"
        ) from error
    if not 0 <= prefix <= 32:
        raise ValueError(
            "EGRESS_EXTRA_ALLOW_MIN_PREFIX must be an integer between 0 and 32"
        )
    return max(prefix, _ABSOLUTE_MIN_PREFIX)


def _extra_allow_redline_networks():
    """返回不可被 extra_allow 等于或覆盖的环境网络。"""
    import ipaddress as _ip

    tenant_supernet = os.environ.get("TENANT_SUPERNET", "").strip()
    if not tenant_supernet:
        subnet_prefix = (
            os.environ.get("VM_SUBNET_PREFIX", "").strip()
            or str(getattr(clients, "VM_SUBNET_PREFIX", "")).strip()
        )
        tenant_supernet = f"{subnet_prefix}.0.0/16" if subnet_prefix else ""
    configured = (
        (
            "VPC_CIDR",
            os.environ.get("EGRESS_VPC_CIDR", "").strip()
            or os.environ.get("VPC_CIDR", "").strip(),
        ),
        ("TENANT_SUPERNET", tenant_supernet),
    )
    networks = []
    for name, raw in configured:
        if not raw:
            continue
        try:
            network = _ip.ip_network(raw, strict=False)
        except ValueError as error:
            raise ValueError(f"{name} must be a valid IPv4 CIDR") from error
        if network.version != 4:
            raise ValueError(f"{name} must be IPv4")
        networks.append((name, network))
    return tuple(networks)


def _admit_allow_entry(entry, index, min_prefix, redline_networks):
    """判定单条 allow[i]。返回 (token, error, criterion)。

    token 是编码后的 `proto:dport:cidr`(判过才有),error 是给调用方的错误串
    (与 #594/#631/#636 已断言的措辞逐字节相同),criterion 是机器可读的判据名。
    """
    import ipaddress as _ip

    if not isinstance(entry, dict):
        return None, f"allow[{index}] must be an object", "entry_not_object"
    proto = str(entry.get("proto", "")).strip().lower()
    if proto not in ("tcp", "udp"):
        return (
            None,
            f"allow[{index}].proto must be tcp|udp",
            "proto_not_tcp_udp",
        )
    dport = entry.get("dport")
    if not (
        isinstance(dport, int) and not isinstance(dport, bool)
    ):
        return (
            None,
            f"allow[{index}].dport must be an integer",
            "dport_not_integer",
        )
    if not 1 <= dport <= 65535:
        return (
            None,
            f"allow[{index}].dport out of range",
            "dport_out_of_range",
        )
    dst = str(entry.get("dst", "")).strip()
    if not dst:
        return (
            None,
            f"allow[{index}].dst is required (IP/CIDR)",
            "dst_required",
        )
    try:
        net = _ip.ip_network(dst, strict=False)
    except ValueError:
        return (
            None,
            f"allow[{index}].dst must be IP/CIDR",
            "dst_not_ip_cidr",
        )
    # HIGH fix — 链是 IPv4-only iptables;放过 IPv6 dst 会让 host 侧 apply 失败、整链换入
    # 中止,且毒 token 被 DDB 持久化 → reconcile 永久卡 / fresh-host 静默 fail-open。拒 IPv6。
    if net.version != 4:
        return (
            None,
            f"allow[{index}].dst must be IPv4 (chain is IPv4-only)",
            "dst_not_ipv4",
        )
    if _ip.ip_address(_IMDS) in net:
        return (
            None,
            f"allow[{index}] must not open IMDS ({_IMDS})",
            "dst_covers_imds",
        )
    if dport in _EXTRA_ALLOW_REDLINE_PORTS:
        return (
            None,
            f"allow[{index}].dport {dport} is an egress red-line port",
            "dport_redline",
        )
    if net.prefixlen < min_prefix:
        return (
            None,
            (
                f"allow[{index}].dst prefix /{net.prefixlen} is broader than "
                f"EGRESS_EXTRA_ALLOW_MIN_PREFIX /{min_prefix}"
            ),
            "dst_prefix_too_broad",
        )
    for name, protected in redline_networks:
        if net == protected or net.supernet_of(protected):
            return (
                None,
                (
                    f"allow[{index}].dst must not equal or contain {name} "
                    f"{protected}"
                ),
                "dst_covers_protected_network",
            )
    return f"{proto}:{dport}:{net}", None, None


def _build_extra_allow(allow):
    """把 API 的 allow=[{proto,dport,dst}] 校验并编码成 EGRESS_EXTRA_ALLOW 串。

    护栏:proto∈tcp/udp;dport 1-65535;dst 必填且为 IP/CIDR(拒绝无目的地放行);
    拒绝对 IMDS 开洞。返回 (str, error)。

    逐条判定在 `_admit_allow_entry`(与 dry-run 端点同一份实现)。这里保持【首错短路】:
    调用方拿到的 400 只报第一条,与 #660 那批真机反验用例的错误串一致;dry-run 那边
    才逐条全判。
    """
    if allow is None:
        return "", None
    if not isinstance(allow, list):
        return "", "allow must be a list of {proto,dport,dst}"
    # 作用域参数与环境网段解析一次即可,放在循环里会让多条 allow 重复解析。
    try:
        min_prefix = _extra_allow_min_prefix()
        redline_networks = _extra_allow_redline_networks()
    except ValueError as error:
        return "", str(error)
    toks = []
    for i, e in enumerate(allow):
        token, error, _criterion = _admit_allow_entry(
            e, i, min_prefix, redline_networks
        )
        if error:
            return "", error
        toks.append(token)
    return ",".join(toks), None


def _summarize_allow_scope(encoded_extra_allow, min_prefix):
    """把这次放行洞的量级显式化;空串表示无洞。"""
    import ipaddress as _ip

    if not encoded_extra_allow:
        return None
    entries = []
    total_addresses = 0
    for token in encoded_extra_allow.split(","):
        _proto, _dport, dst = token.split(":", 2)
        network = _ip.ip_network(dst, strict=False)
        addresses = network.num_addresses
        entries.append(
            {
                "rule": token,
                "prefix": network.prefixlen,
                "addresses": addresses,
            }
        )
        total_addresses += addresses
    entries.sort(key=lambda item: item["prefix"])
    widest_prefix = entries[0]["prefix"]
    return {
        "entries": entries,
        "widest_prefix": widest_prefix,
        "total_addresses": total_addresses,
        "below_default_floor": widest_prefix < _DEFAULT_MIN_PREFIX,
        "min_prefix": min_prefix,
    }


def fleet_egress_allow_validate(body=None, event=None):
    """POST /hosts/egress/allow/validate — 只读 dry-run:逐条给出判定与当前阈值。

    与 POST /hosts/egress 的 admission 同源,但逐条全判不短路,且绝不写 DDB、
    不发 SSM、不落 revision。权限与 POST /hosts/egress 同门(admin)。
    """
    import ipaddress as _ip

    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403,
            {
                "error": "forbidden: fleet egress admin requires admin",
                "required": "admin",
            },
        )
    try:
        body = json.loads(body) if isinstance(body, str) else (body or {})
    except (TypeError, ValueError):
        return _resp(400, {"error": "body must be a JSON object"})
    if not isinstance(body, dict):
        return _resp(400, {"error": "body must be a JSON object"})

    allow = body.get("allow")
    if allow is None:
        return _resp(
            400,
            {"error": "allow is required (a list of {proto,dport,dst})"},
        )
    if not isinstance(allow, list):
        return _resp(
            400, {"error": "allow must be a list of {proto,dport,dst}"}
        )
    if len(allow) > _VALIDATE_MAX_ENTRIES:
        return _resp(
            400,
            {
                "error": (
                    f"allow must contain at most {_VALIDATE_MAX_ENTRIES} entries"
                ),
                "got": len(allow),
            },
        )
    try:
        min_prefix = _extra_allow_min_prefix()
        redline_networks = _extra_allow_redline_networks()
    except ValueError as error:
        # 环境配置本身非法时不能装作能判 —— 与 POST 路径同源地报 400。
        return _resp(400, {"error": str(error)})

    protected_networks = {
        name: str(network) for name, network in redline_networks
    }
    results = []
    accepted_tokens = []
    warnings = []
    for index, entry in enumerate(allow):
        token, error, criterion = _admit_allow_entry(
            entry, index, min_prefix, redline_networks
        )
        if error:
            results.append(
                {
                    "index": index,
                    "verdict": "reject",
                    "rule": None,
                    "criterion": criterion,
                    "error": error,
                    "prefix": None,
                }
            )
            continue
        accepted_tokens.append(token)
        network = _ip.ip_network(token.split(":", 2)[2], strict=False)
        prefix = network.prefixlen
        results.append(
            {
                "index": index,
                "verdict": "accept",
                "rule": token,
                "criterion": None,
                "error": None,
                "prefix": prefix,
            }
        )
        if prefix < _DEFAULT_MIN_PREFIX:
            warnings.append(
                f"allow[{index}] opens /{prefix} ({network.num_addresses} addresses); "
                "wider than the /24 default floor — the caller owns this blast "
                "radius (#668)"
            )
        if prefix <= _ABSOLUTE_MIN_PREFIX:
            warnings.append(
                f"allow[{index}] /{prefix} may cover an entire VPC; oc-egress-sim's "
                "semantic probe needs one in-VPC address left outside every hole and "
                "fails closed when the holes consume that probe space, so host-side "
                "apply can exit non-zero while the desired state is already "
                "persisted. The host then reports 'redline reachable: allow holes "
                "cover the entire VPC_CIDR' — grep that string on the host to "
                "confirm (ADR-egress-allow-hole-redline §10)"
            )
    # 逐条报「哪条判据在本环境不可用」,而不是只在两条都缺时报一句。
    # 真机实测(us-west-2 openclaw-api,83 项 env):没有 VPC_CIDR/EGRESS_VPC_CIDR,但有
    # VM_SUBNET_PREFIX,于是 TENANT_SUPERNET 是【派生出来的】而 VPC_CIDR 那条判据恒不生效。
    # 只在 protected_networks 全空时才 warn 的写法,在真机上永远不会触发 —— 而"覆盖 VPC
    # 网段那条拦不住"恰好是运维最该知道的一件事,也正是绝对前缀下界存在的理由。
    if "VPC_CIDR" not in protected_networks:
        warnings.append(
            "VPC_CIDR/EGRESS_VPC_CIDR is not configured in this environment, so the "
            "'dst must not equal or contain VPC_CIDR' check cannot run here — a hole "
            "that covers the whole VPC will pass admission and be caught only by the "
            "host-side semantic probe. The absolute prefix floor "
            f"(/{_ABSOLUTE_MIN_PREFIX}) is what constrains scope here"
        )
    if "TENANT_SUPERNET" not in protected_networks:
        warnings.append(
            "TENANT_SUPERNET is not configured and could not be derived from "
            "VM_SUBNET_PREFIX, so the cross-tenant 'dst must not equal or contain "
            "TENANT_SUPERNET' check cannot run here"
        )

    accepted_count = len(accepted_tokens)
    rejected_count = len(allow) - accepted_count
    all_accepted = rejected_count == 0
    encoded_extra_allow = ",".join(accepted_tokens) if all_accepted else ""
    return _resp(
        200,
        {
            "verdict": "accept" if all_accepted else "reject",
            "allow_count": len(allow),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "results": results,
            "criteria": {
                "min_prefix": min_prefix,
                "default_min_prefix": _DEFAULT_MIN_PREFIX,
                "absolute_min_prefix": _ABSOLUTE_MIN_PREFIX,
                "min_prefix_env": os.environ.get(
                    "EGRESS_EXTRA_ALLOW_MIN_PREFIX"
                ),
                "ipv4_only": True,
                "imds": _IMDS,
                "redline_ports": sorted(_EXTRA_ALLOW_REDLINE_PORTS),
                "protected_networks": protected_networks,
                "proto": ["tcp", "udp"],
                "dport_range": [1, 65535],
            },
            "encoded_extra_allow": encoded_extra_allow,
            "extra_allow_scope": (
                _summarize_allow_scope(encoded_extra_allow, min_prefix)
                if all_accepted
                else None
            ),
            "warnings": warnings,
            "side_effects": "none",
            "message": (
                "dry-run only: this endpoint never writes desired state, never "
                "dispatches SSM, and never records a revision"
            ),
        },
    )

# 在每台 host 上跑的一段(读该 host 真实 /etc/platform.env 派生 LiteLLM allow 洞 + apply/
# teardown + 回读 OPENCLAW-EGRESS 的 sha256 供逐机一致性核对)。__MODE__ /
# __ENFORCE_PIN__ 由调用方替换。
# 空 host(无 guest)无 conntrack anchor → 补幂等前置;真实 host 有 guest 时前置是 no-op。
_ON_HOST = r'''
set -u
MODE="__MODE__"; DENY_RFC1918="__DENY_RFC1918__"
B=$(grep -E '^ASSETS_BUCKET=' /etc/platform.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')
[ -n "$B" ] || B=$(grep -oE 'openclaw-assets-[0-9]+' /etc/platform.env 2>/dev/null | head -1)
R=$(grep -E '^OC_REGION=' /etc/platform.env | cut -d= -f2 | tr -d '"'); [ -z "$R" ] && R=__REGION__
aws s3 cp "s3://$B/deployment/scripts/oc-egress-chain.sh" /home/ubuntu/oc-egress-chain.sh --region "$R" --quiet
aws s3 cp "s3://$B/deployment/scripts/oc-egress-sim.py"  /home/ubuntu/oc-egress-sim.py  --region "$R" --quiet
chmod +x /home/ubuntu/oc-egress-chain.sh
# __ENFORCE_PIN__ 为 true 时(= 本次是全量且 mode 不是 off),先读自己的 host 行:
# 显式 pin 的机器拒绝执行。强制点放在 host 侧而非控制面 target 过滤 —— tag Targets
# 无法排除实例,InstanceIds 又有 50 上限,且这样任何路径(含手写 SSM)都被同一道门挡住。
#
# mode=off 时【不】走这段:机队熔断在写入时刻赢(§2.3 单向语义)。之后一次定向写
# 仍会按时间戳把那台机器拉回 host 行的 mode —— 熔断不等于永久钉死。
if [ "__ENFORCE_PIN__" = "true" ]; then
  IID=$(grep -E '^INSTANCE_ID=' /etc/platform.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')
  HT=$(grep -E '^HOSTS_TABLE=' /etc/platform.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')
  if [ -z "$IID" ] || [ -z "$HT" ]; then
    echo "PIN_CHECK=unavailable reason=missing_platform_env"
  else
    # 严判据:只认真正的 DDB BOOL true。--output text 分不清 BOOL true 与 S "true",
    # 而 S "false" 在 Python 侧 bool() 为真 → 两侧方向相反,链会来回跳(§2.1.1 真机实测)。
    PIN_JSON=$(aws dynamodb get-item --region "$R" --table-name "$HT" \
      --key "{\"instance_id\":{\"S\":\"$IID\"}}" --consistent-read \
      --projection-expression egress_pinned --output json 2>/dev/null)
    PIN_RC=$?
    if [ $PIN_RC -ne 0 ]; then
      # fail-open 继续执行(否则一次 DDB 抖动 = 全机队集体跳过下发),但必须【可见】。
      echo "PIN_CHECK=unavailable reason=getitem_rc_${PIN_RC}"
    else
      echo "PIN_CHECK=ok"
      if [ -n "$(printf '%s' "$PIN_JSON" | tr -d ' \n' | grep -o '\"BOOL\":true' | head -1)" ]; then
        echo "PINNED_SKIP=1"
        echo "APPLY_EXIT=0"
        echo "RULES_SHA256=$(iptables -S OPENCLAW-EGRESS 2>/dev/null | sha256sum | cut -d' ' -f1)"
        exit 0
      fi
    fi
  fi
fi
if [ "$MODE" = "off" ]; then
  # APPLY_EXIT 必须是真实退出码。旧写法是 `... || true` + 字面 echo "APPLY_EXIT=0",
  # 于是【唯一那条 break-glass 路径】把失败也报成成功:上面两条 aws s3 cp 是 --quiet
  # 且不查退出码,桶名/IAM/限流任一出问题 → 脚本不存在 → bash 退 127 → `|| true`
  # 吞掉 → 控制面按 APPLY_EXIT=0 判 command_ok → 整个机队熔断返回 {"ok":true} 200,
  # 而链其实还在、那些租户的 guest 还是被拦着。deny 分支(下面 APPLY_EXIT=$?)一直是
  # 诚实的,所以这是同一 feature 内部的不一致。teardown_chain 本身幂等(无链时也退 0),
  # 换成真实退出码不会给"本来就 off"的机器造假红。
  TAP_IFACE="tap+" bash /home/ubuntu/oc-egress-chain.sh teardown 2>&1
  echo "APPLY_EXIT=$?"
  echo "RULES_SHA256=$(iptables -S OPENCLAW-EGRESS 2>/dev/null | sha256sum | cut -d' ' -f1)"
  exit 0
fi
DERIVED=$(python3 - <<'PY'
import ipaddress, os, socket
from urllib.parse import urlparse
env = {}
for l in open("/etc/platform.env"):
    l = l.strip()
    if "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1); env[k] = v.strip().strip('"')
vpc = env.get("EGRESS_VPC_CIDR", ""); raw = env.get("LITELLM_HOST", "")
llm_cidr = env.get("EGRESS_LLM_CIDR", ""); subnet_prefix = env.get("SUBNET_PREFIX", "")
tenant_supernet = f"{subnet_prefix}.0.0/16" if subnet_prefix else ""
u = urlparse(raw if "://" in raw else "//" + raw, scheme=""); host = u.hostname or ""; port = u.port
sch = (u.scheme or "").lower()
if not port:
    port = 443 if sch == "https" else (80 if sch == "http" else 4000)
ip = ""
if not llm_cidr:
    try:
        ip = socket.gethostbyname(host) if host else ""
    except Exception:
        ip = ""
inv = False
try:
    inv = ip and ipaddress.ip_address(ip) in ipaddress.ip_network(vpc, strict=False)
except Exception:
    inv = False
print(f"{vpc}|{ip if inv and not llm_cidr else ''}|{port}|{llm_cidr}|{tenant_supernet}")
PY
)
IFS='|' read -r VPC LLM_IP LLM_PORT LLM_CIDR TENANT_SUPERNET <<<"$DERIVED"
iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
ip link show tap0 >/dev/null 2>&1 || { ip tuntap add tap0 mode tap; ip link set tap0 up; }
VPC_CIDR="$VPC" LITELLM_HOST="$LLM_IP" LITELLM_CIDR="$LLM_CIDR" LITELLM_PORT="$LLM_PORT" TENANT_SUPERNET="$TENANT_SUPERNET" SPIRE_SERVER="" TAP_IFACE="tap+" DENY_RFC1918="$DENY_RFC1918" EGRESS_EXTRA_ALLOW="__EXTRA_ALLOW__" \
  bash /home/ubuntu/oc-egress-chain.sh apply
echo "APPLY_EXIT=$?"
echo "RULES_SHA256=$(iptables -S OPENCLAW-EGRESS 2>/dev/null | sha256sum | cut -d' ' -f1)"
'''


def _enumerate_hosts(targets, include_rows=False):
    """指定 targets(instance_id 列表)则用之;否则强一致扫全部 active/idle host。"""
    if isinstance(targets, list) and targets:
        instance_ids = [str(t) for t in targets]
        return (instance_ids, []) if include_rows else instance_ids
    if include_rows:
        # 带 filter,不要扫全表:该表保留软删行(apse1 实测 239 行里 219 个 status=deleted),
        # 无 filter 会让批量 unpin 去逐个 update 那些早已终止的实例。
        # 含 draining:那类 host 仍承载租户、仍在跑 host-agent,它上面残留的 pin 仍然生效,
        # 批量解除必须覆盖到;而下发目标 instance_ids 仍只取 active/idle(与原行为一致)。
        rows = ddb_scan.scan_all(
            clients.hosts_table,
            FilterExpression="#s IN (:a, :i, :d)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":a": "active",
                ":i": "idle",
                ":d": "draining",
            },
            ConsistentRead=True,
        )
        host_rows = [
            row
            for row in rows
            if _is_dispatchable_host_id(row.get("instance_id"))
        ]
        instance_ids = [
            str(row["instance_id"])
            for row in host_rows
            if row.get("status") in ("active", "idle")
        ]
        return instance_ids, host_rows
    hosts = ddb_scan.scan_all(
        clients.hosts_table,
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    )
    return [
        h["instance_id"]
        for h in hosts
        if _is_dispatchable_host_id(h.get("instance_id"))
    ]


def _read_host_rows(instance_ids):
    """强一致读指定 host 行。定向路径 ≤50 台,逐个 get_item 足够且不需要 scan 全表。"""
    rows = []
    for iid in instance_ids:
        try:
            item = clients.hosts_table.get_item(
                Key={"instance_id": str(iid)}, ConsistentRead=True
            ).get("Item")
        except Exception as e:  # noqa: BLE001
            print(f"egress host row read failed for {iid}: {e}")
            item = None
        if item:
            rows.append(item)
    return rows


def _read_host_rows_strict(instance_ids):
    """强一致读回滚目标;任何读失败都上浮,不能把"读不到"误当成"没有 host 行"。"""
    rows = []
    for iid in instance_ids:
        item = clients.hosts_table.get_item(
            Key={"instance_id": str(iid)}, ConsistentRead=True
        ).get("Item")
        if item:
            rows.append(item)
    return rows


def _read_fleet_policy():
    """Read the fleet egress singleton with strong consistency."""
    return (
        clients.hosts_table.get_item(
            Key={"instance_id": "__fleet_egress_policy__"},
            ConsistentRead=True,
        ).get("Item")
        or {}
    )


def _classify_host(host, desired):
    """Classify one host against the selected fleet desired state."""
    if not host.get("egress_applied_mode"):
        return False, "never_reported"
    if host.get("egress_reconcile_error"):
        return False, "reconcile_error"
    if host.get("egress_applied_mode") != desired["mode"]:
        return False, "mode_mismatch"
    if str(host.get("egress_applied_version")) != str(desired["policy_version"]):
        return False, "version_mismatch"
    return True, None


def _classify_against_own_row(host):
    """按 host 行自己的期望态判收敛(pinned 豁免与普通定向灰度共用)。

    不能用 _classify_host:那个拿单例的 policy_version 比,而 host 行没有该字段,
    host-agent 会上报 egress_desired_at 这个 ISO 串当版本 —— 两者永不相等。
    """
    return (
        host.get("egress_mode") in VALID_MODES
        and host.get("egress_applied_mode") == host.get("egress_mode")
        and str(host.get("egress_applied_version"))
        == str(host.get("egress_desired_at"))
        and not host.get("egress_reconcile_error")
    )


def _write_desired_state_detailed(
    instance_ids, mode, deny_rfc1918, extra_allow="", pinned=None
):
    """声明式期望态逐台写 DDB,返回 `(written, failed_ids)`。best-effort。

    同时持久化 egress_extra_allow,让 host-agent reconcile 在 reboot/重建后连同额外放行洞
    一起收敛(否则重启只恢复 deny 基线、丢掉运维加的端口)。
    """
    now = _now()
    written = 0
    failed_ids = []
    for iid in instance_ids:
        try:
            update_expression = (
                "SET egress_mode = :m, egress_deny_rfc1918 = :r, "
                "egress_extra_allow = :x, egress_desired_at = :t"
            )
            values = {
                ":m": mode,
                ":r": bool(deny_rfc1918),
                ":x": extra_allow or "",
                ":t": now,
            }
            if pinned is True:
                update_expression += ", egress_pinned = :p"
                values[":p"] = True
            elif pinned is False:
                update_expression += " REMOVE egress_pinned"
            clients.hosts_table.update_item(
                Key={"instance_id": iid},
                UpdateExpression=update_expression,
                # 镜像 taint 先例:只写已存在的 host 行,绝不 upsert 幽灵行。
                ConditionExpression="attribute_exists(instance_id)",
                ExpressionAttributeValues=values,
            )
            written += 1
        except Exception as e:  # noqa: BLE001 — 期望态 best-effort,不阻断 live-apply
            print(f"egress desired-state write failed {iid}: {e}")
            failed_ids.append(str(iid))
    return written, failed_ids


def _write_desired_state(
    instance_ids, mode, deny_rfc1918, extra_allow="", pinned=None
):
    """声明式期望态写 DDB openclaw-hosts(taint 模式,扛 host 重建)。best-effort。

    同时持久化 egress_extra_allow,让 host-agent reconcile 在 reboot/重建后连同额外放行洞
    一起收敛(否则重启只恢复 deny 基线、丢掉运维加的端口)。

    保留既有计数契约;需要逐台失败明细的回滚路径调用 detailed 版本。
    """
    written, _failed_ids = _write_desired_state_detailed(
        instance_ids,
        mode,
        deny_rfc1918,
        extra_allow,
        pinned=pinned,
    )
    return written


def _remove_egress_pins(host_rows):
    """REMOVE egress_pinned from every non-singleton host row that actually has one.

    返回 `(removed, failed, skipped_no_pin)`。

    为什么失败必须【上浮】而不是只打日志:批量解除是「>50 台时唯一可用的解除调用」
    (InstanceIds 上限 50,见 §2.2)。一台 update 失败 = 那台 host 仍带 pin,而 pin 会
    持续否决后续所有例行全量下发。若只返回一个 removed 计数,响应体报 `ok: true` +
    一个偏小的 `unpinned_count`,从外面【无法与"本来就只有这么多台带 pin"区分】——
    运维以为解除完了,实际留了 N 台永久豁免的机器。生产默认 `wait=false` 连
    `hosts[]` 都没有,更查不到。

    `skipped_no_pin` 让 `unpinned_count` 说的是「真的清掉了几个 pin」而不是「改了几行」:
    原实现对每一行都发 update,300 台的机队恒报 300 并真发 300 次写。
    """
    removed = 0
    failed = []
    skipped_no_pin = 0
    for host in host_rows:
        iid = str(host.get("instance_id") or "")
        if not _is_dispatchable_host_id(iid):
            continue
        # 第二层守卫:扫描已带 status filter,但软删行(status=deleted)绝不该被 update ——
        # 那些实例早已终止,instance_id 不复用,清它的 pin 没有意义只有 API 调用成本。
        # 两层都守,是为了「日后有人去掉扫描的 filter」时行为不退化。
        if host.get("status") == "deleted":
            continue
        if "egress_pinned" not in host:
            skipped_no_pin += 1
            continue
        try:
            clients.hosts_table.update_item(
                Key={"instance_id": iid},
                UpdateExpression="REMOVE egress_pinned",
                ConditionExpression="attribute_exists(instance_id)",
            )
            removed += 1
        except Exception as e:  # noqa: BLE001 — 逐台不中断,但失败要进返回值
            print(f"egress batch unpin failed {iid}: {e}")
            failed.append(iid)
    return removed, sorted(failed), skipped_no_pin


def _write_fleet_policy(mode, deny_rfc1918, extra_allow=""):
    """Upsert the fleet egress singleton used by newly launched hosts."""
    clients.hosts_table.update_item(
        Key={"instance_id": "__fleet_egress_policy__"},
        UpdateExpression=(
            "SET egress_mode = :m, egress_deny_rfc1918 = :r, "
            "egress_extra_allow = :x, policy_version = :v, updated_at = :t"
        ),
        ExpressionAttributeValues={
            ":m": mode,
            ":r": bool(deny_rfc1918),
            ":x": extra_allow or "",
            ":v": int(time.time() * 1000),
            ":t": _now(),
        },
    )


def _clear_fleet_policy():
    """把 fleet 单例恢复为“从未设置过”,但保留一次可观察的版本推进。"""
    # 不能删整行或只 REMOVE 策略字段:host-agent 还要靠 version/updated_at 看出单例变过。
    # 条件写禁止在单例行不存在时凭空创建一条只有版本号、没有期望态的残缺记录。
    clients.hosts_table.update_item(
        Key={"instance_id": "__fleet_egress_policy__"},
        UpdateExpression=(
            "SET policy_version = :v, updated_at = :t "
            "REMOVE egress_mode, egress_deny_rfc1918, egress_extra_allow"
        ),
        ExpressionAttributeValues={
            ":v": int(time.time() * 1000),
            ":t": _now(),
        },
        ConditionExpression="attribute_exists(instance_id)",
    )


# ---------------------------------------------------------------------------
# 命名版本 + 逐台回滚(#603 增量)
# ---------------------------------------------------------------------------
# 每次下发都留一条【命名版本】,记录:改之前那份期望态(用来回滚)+ 这次要写的期望态
# + 作用域。回滚就是把某个版本记录里的 before 重新下发给指定的机器。
#
# 为什么按 host 存 before 而不是只存一份 fleet 快照:作用域可以是"只发选中的几台",
# 那几台改之前各自的期望态可能不同(有 pin 的、有做过定向灰度的)。只存一份 fleet 快照
# 的话,回滚会把它们全拉成同一份 —— 那不是回滚,是又一次全量下发。
_REV_PREFIX = "__egress_rev__"


def _rev_key(name):
    return f"{_REV_PREFIX}{name}"


def _list_revisions():
    """按 created_at 倒序列出所有命名版本(新的在前)。"""
    # 必须带 filter。这张表同时放着全部 host 行(apse1 实测 239 行,含 219 个软删),
    # 而本函数在【每次 POST】都要跑一次去算下一个自动版本号 —— 无 filter 全表扫会把每台
    # host 的完整 item 都拉回来只为挑出几条版本行。begins_with 走在服务端,省的是回传量
    # (Scan 的读容量按扫过的数据算,filter 不省 RCU,但省网络与反序列化)。
    rows = ddb_scan.scan_all(
        clients.hosts_table,
        FilterExpression="begins_with(instance_id, :revpfx)",
        ExpressionAttributeValues={":revpfx": _REV_PREFIX},
    )
    revs = []
    for row in rows:
        iid = str(row.get("instance_id") or "")
        # 服务端已按前缀过滤,这里再判一次:filter 万一被改掉,解析逻辑不该跟着漏。
        if not iid.startswith(_REV_PREFIX):
            continue
        raw_before_incomplete = row.get("before_incomplete")
        revs.append(
            {
                "name": iid[len(_REV_PREFIX):],
                "created_at": row.get("created_at") or "",
                "author": row.get("author") or "",
                "scope": row.get("scope") or "all",
                "targets": list(row.get("targets") or []),
                "spec": dict(row.get("spec") or {}),
                "before": {k: dict(v) for k, v in (row.get("before") or {}).items()},
                # 安全侧熔断标记按 truthy 收紧;同时保留原始类型名供回滚拒绝时诊断。
                "before_incomplete": bool(raw_before_incomplete),
                "before_incomplete_raw_type": (
                    type(raw_before_incomplete).__name__
                    if raw_before_incomplete is not None
                    else None
                ),
            }
        )
    revs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return revs


def _next_revision_name(existing_names):
    """没给名字时自动取 v1 / v2 / v3 …

    只认 `v<纯数字>` 这一种形状去推下一个;运维自己起的名字(如 "open-litellm")不参与
    编号,免得 "v2-hotfix" 这类名字把计数器带偏。
    """
    nums = []
    for n in existing_names:
        if len(n) > 1 and n[0] == "v" and n[1:].isdigit():
            nums.append(int(n[1:]))
    return f"v{(max(nums) + 1) if nums else 1}"


_REV_NAME_MAX = 48


def _validate_revision_name(raw):
    """版本名会进 DDB 主键,也会在 portal 上显示 —— 收紧字符集,避免撞上 __ 前缀。"""
    name = str(raw or "").strip()
    if not name:
        return None, "revision name must not be empty when provided"
    if len(name) > _REV_NAME_MAX:
        return None, f"revision name must be at most {_REV_NAME_MAX} characters"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None, (
            "revision name may only contain letters, digits, dot, underscore and hyphen"
        )
    if name.startswith("__"):
        return None, "revision name must not start with '__' (reserved for internal rows)"
    return name, None


def _snapshot_before(instance_ids, host_rows, fleet_policy, is_all):
    """记下"改之前"的期望态,按 host 存。

    全量时每台 host 的生效期望态 = 它自己的 host 行(若有 egress_mode)否则 fleet 单例 ——
    与 host-agent 的 _egress_policy_source 同口径,否则回滚会把"本来听 fleet 的机器"
    错误地钉成 host 行。
    """
    by_id = {str(h.get("instance_id")): h for h in (host_rows or [])}
    # fleet_policy is None 表示【读不到】,与 {} 的"从未设置过"是两件事。前者不能记成
    # source=none —— 那会让将来的回滚把一台其实在跑 deny 的机器写成 off。记 unknown,
    # 让回滚对这些机器直接 409 拒绝(fleet_egress_rollback 的 else 分支)。
    fleet_unreadable = fleet_policy is None
    fleet = fleet_policy or {}
    before = {}
    for iid in instance_ids:
        row = by_id.get(str(iid)) or {}
        if row.get("egress_mode"):
            src, pol = "host", row
        elif fleet.get("egress_mode"):
            src, pol = "fleet", fleet
        elif fleet_unreadable:
            src, pol = "unknown", {}
        else:
            src, pol = "none", {}
        before[str(iid)] = {
            "source": src,
            "mode": pol.get("egress_mode") or "",
            "deny_rfc1918": bool(pol.get("egress_deny_rfc1918", False)),
            "extra_allow": pol.get("egress_extra_allow") or "",
            "pinned": row.get("egress_pinned") is True,
        }
    if is_all:
        before["__fleet__"] = {
            "source": "unknown" if fleet_unreadable else "fleet",
            "mode": fleet.get("egress_mode") or "",
            "deny_rfc1918": bool(fleet.get("egress_deny_rfc1918", False)),
            "extra_allow": fleet.get("egress_extra_allow") or "",
            "pinned": False,
        }
    return before


def _write_revision(name, author, is_all, targets, spec, before, before_incomplete=False):
    """落一条版本记录。用 attribute_not_exists 防同名覆写 —— 覆写等于把可回滚的历史弄丢。"""
    clients.hosts_table.put_item(
        Item={
            "instance_id": _rev_key(name),
            "created_at": _now(),
            "author": str(author or "")[:128],
            "scope": "all" if is_all else "targeted",
            "targets": [] if is_all else [str(t) for t in targets],
            "spec": spec,
            "before": before,
            # 这条版本的 before 是否可信。break-glass 在读不到期望态时仍放行下发,
            # 那次留下的记录只是"有这么一次变更"的账,不能当回滚点用 —— 不标出来的话
            # 它在版本列表里与真锚点长得一模一样。
            "before_incomplete": bool(before_incomplete),
        },
        ConditionExpression="attribute_not_exists(instance_id)",
    )


def _record_revision(
    raw_name,
    ident,
    is_all,
    instance_ids,
    host_rows,
    fleet_before,
    spec,
    kind,
    include_fleet_snapshot=False,
):
    """落一条命名版本。返回 (revision_dict, error) —— error 为 (status, body) 或 None。

    fail-closed:记不下回滚点就不下发。一次「改错了但回不去」的全机队变更,比一次 500
    严重得多,而调用方看到 500 时期望态与内核都还没动,重试是安全的。

    唯一例外是 mode=off 这条 break-glass 路径:熔断不能被一次 DDB 抖动挡住(§2.3 单向
    语义),所以那条走 best-effort,失败只在响应里报 revision_error。
    """
    break_glass = spec.get("mode") == "off"
    existing = []
    try:
        existing = [r["name"] for r in _list_revisions()]
    except Exception as e:  # noqa: BLE001
        if not break_glass:
            return None, (
                503,
                {
                    "error": f"cannot list revisions, refusing to dispatch: {e}",
                    "hint": "无法确认回滚点是否可写入时不下发;重试是安全的(期望态与内核都未改动)",
                },
            )
        print(f"egress revision list failed (break-glass, continuing): {e}")

    auto = raw_name is None or str(raw_name).strip() == ""
    if auto:
        name = _next_revision_name(existing)
    else:
        name, err = _validate_revision_name(raw_name)
        if err:
            return None, (400, {"error": err, "next_auto_name": _next_revision_name(existing)})
        if name in existing:
            return None, (
                409,
                {
                    "error": f"revision name already exists: {name}",
                    "hint": "版本名是回滚的唯一坐标,重名会把可回滚的历史覆写掉",
                    "next_auto_name": _next_revision_name(existing),
                },
            )

    # fleet_before is None = 改动前的期望态没读到(只有 break-glass 会走到这里)。
    before_incomplete = fleet_before is None
    # 定向 rollback 也可能显式改 fleet 单例;那时 scope 仍是 targeted,但回滚点必须把
    # 即将被改写的当前单例一起拍下,否则 rollback-of-rollback 无法恢复这次单例改动。
    snapshot_includes_fleet = is_all or include_fleet_snapshot
    before = _snapshot_before(
        instance_ids,
        host_rows,
        fleet_before,
        snapshot_includes_fleet,
    )
    payload = {
        "kind": kind,
        "mode": spec.get("mode"),
        "deny_rfc1918": bool(spec.get("deny_rfc1918")),
        "extra_allow": spec.get("extra_allow") or "",
        "pinned": spec.get("pinned"),
    }
    author = str(ident.get("sub") or ident.get("user_id") or ident.get("caller") or "")
    # 自动名遇上并发撞号就往后挪(两个运维同时下发时 max+1 可能算出同一个值);
    # 显式名不重试 —— 那是调用方的坐标,悄悄改成 v8 会让他之后按名字回滚时找错版本。
    attempts = 3 if auto else 1
    last_error = None
    for i in range(attempts):
        candidate = name if i == 0 else _next_revision_name(existing + [name])
        try:
            _write_revision(
                candidate,
                author,
                is_all,
                instance_ids,
                payload,
                before,
                before_incomplete=before_incomplete,
            )
            return (
                {
                    "name": candidate,
                    "auto_named": auto,
                    "scope": "all" if is_all else "targeted",
                    "hosts_recorded": len(before) - (
                        1 if snapshot_includes_fleet else 0
                    ),
                    "kind": kind,
                    "before_incomplete": before_incomplete,
                },
                None,
            )
        except Exception as e:  # noqa: BLE001
            last_error = e
            existing.append(candidate)
            name = candidate
    if break_glass:
        print(f"egress revision write failed (break-glass, continuing): {last_error}")
        return {"name": None, "error": str(last_error)}, None
    return None, (
        503,
        {
            "error": f"cannot record rollback revision, refusing to dispatch: {last_error}",
            "hint": "期望态与内核均未改动;重试是安全的",
        },
    )


def fleet_egress_revisions(event=None):
    """GET /hosts/egress/revisions — 列出命名版本,供 portal 做逐台回滚。"""
    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403, {"error": "forbidden: fleet egress admin requires admin", "required": "admin"}
        )
    query = (event or {}).get("queryStringParameters") or {}
    raw_limit = query.get("limit", 50)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return _resp(
            400, {"error": "limit must be an integer between 1 and 200"}
        )
    if not 1 <= limit <= 200:
        return _resp(
            400, {"error": "limit must be an integer between 1 and 200"}
        )

    revs = _list_revisions()
    returned_revs = revs[:limit]
    return _resp(
        200,
        {
            "revisions": returned_revs,
            "revisions_returned": len(returned_revs),
            "revisions_truncated": len(returned_revs) < len(revs),
            "limit": limit,
            "total": len(revs),
            # 自动名是写路径的唯一坐标,必须看全量版本;按展示页推会反复撞已有名字。
            "next_auto_name": _next_revision_name([r["name"] for r in revs]),
            "message": (
                "回滚 = 把某个版本记录里的 before 重新下发给指定机器;"
                "before 是按 host 存的,所以可以只回滚其中几台"
            ),
        },
    )


def _is_conditional_check_failed(error):
    """兼容 boto3 具体异常类与测试替身,只把条件不满足解释成记录不存在。"""
    if type(error).__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(error, "response", None)
    return (
        isinstance(response, dict)
        and (response.get("Error") or {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def fleet_egress_revisions_delete(body=None, event=None):
    """DELETE /hosts/egress/revisions — 逐个点名删除不再需要的回滚版本。"""
    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403, {"error": "forbidden: fleet egress admin requires admin", "required": "admin"}
        )
    try:
        body = json.loads(body) if isinstance(body, str) else (body or {})
    except (TypeError, ValueError):
        return _resp(400, {"error": "body must be a JSON object"})
    if not isinstance(body, dict):
        return _resp(400, {"error": "body must be a JSON object"})

    revisions = body.get("revisions")
    if (
        not isinstance(revisions, list)
        or not revisions
        or not all(isinstance(name, str) and name.strip() for name in revisions)
    ):
        return _resp(
            400,
            {
                "error": (
                    "revisions must be a non-empty list of revision name strings"
                )
            },
        )
    if body.get("confirm") != "DELETE":
        return _resp(400, {"error": 'confirm must be exactly "DELETE"'})

    names = []
    invalid = []
    for raw_name in revisions:
        name, error = _validate_revision_name(raw_name)
        if error:
            invalid.append(raw_name)
        elif name not in names:
            names.append(name)
    # 删除不可逆,所以必须先完成整批校验;不能先删合法项再报告同批里有非法名字。
    if invalid:
        return _resp(
            400,
            {
                "error": "one or more revision names are invalid",
                "invalid": invalid,
            },
        )

    deleted = []
    not_found = []
    for name in names:
        try:
            clients.hosts_table.delete_item(
                Key={"instance_id": _rev_key(name)},
                ConditionExpression="attribute_exists(instance_id)",
            )
            deleted.append(name)
        except Exception as error:  # noqa: BLE001 — 不存在与真实写失败必须分开上浮
            if _is_conditional_check_failed(error):
                not_found.append(name)
                continue
            print(f"egress revision delete failed {name}: {error}")
            return _resp(
                503,
                {
                    "error": f"cannot delete revision {name}: {error}",
                    "deleted": deleted,
                    "not_found": not_found,
                },
            )

    try:
        remaining_total = len(_list_revisions())
    except Exception as error:  # noqa: BLE001 — 删除已发生,剩余数读不到不能伪造成功读数
        print(f"egress revision recount failed after delete: {error}")
        return _resp(
            503,
            {
                "error": f"revisions deleted but remaining total could not be read: {error}",
                "deleted": deleted,
                "not_found": not_found,
            },
        )
    return _resp(
        200,
        {
            "deleted": deleted,
            "not_found": not_found,
            "remaining_total": remaining_total,
            "message": (
                f"deleted {len(deleted)} revision(s); "
                f"{len(not_found)} revision(s) were not found"
            ),
        },
    )


def fleet_egress_status(event=None):
    """GET /hosts/egress — return the read-only fleet convergence report."""
    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403, {"error": "forbidden: fleet egress admin requires admin", "required": "admin"}
        )

    query = (event or {}).get("queryStringParameters") or {}
    raw_limit = query.get("limit")
    limit = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return _resp(400, {"error": "limit must be an integer between 1 and 500"})
        if not 1 <= limit <= 500:
            return _resp(400, {"error": "limit must be an integer between 1 and 500"})

    raw_instance_ids = query.get("instance_ids")
    instance_ids_filter = None
    if raw_instance_ids is not None:
        if not isinstance(raw_instance_ids, str):
            return _resp(400, {"error": "instance_ids must be a comma-separated string"})
        instance_ids_filter = list(
            dict.fromkeys(
                item.strip()
                for item in raw_instance_ids.split(",")
                if item.strip()
            )
        )
        if not instance_ids_filter:
            return _resp(
                400, {"error": "instance_ids must contain at least one instance id"}
            )

    fleet_policy = _read_fleet_policy()
    if fleet_policy:
        desired = {
            "mode": fleet_policy.get("egress_mode"),
            "policy_version": fleet_policy.get("policy_version"),
            "deny_rfc1918": bool(
                fleet_policy.get("egress_deny_rfc1918", False)
            ),
            "extra_allow": fleet_policy.get("egress_extra_allow", "") or "",
            "updated_at": fleet_policy.get("updated_at", ""),
            "source": "fleet",
        }
    else:
        desired = {"mode": None, "source": "per-host"}

    host_rows = ddb_scan.scan_all(
        clients.hosts_table,
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    )
    # 统一判据排掉的是内部行或脏键,它们本来就不是 host,不是静默漏报一台机器。
    host_rows = [
        host
        for host in host_rows
        if _is_dispatchable_host_id(host.get("instance_id"))
    ]

    hosts = []
    outliers = []
    pinned_count = 0
    pinned_malformed_count = 0
    for host in host_rows:
        applied_version = host.get("egress_applied_version")
        pinned = host.get("egress_pinned") is True
        pinned_malformed = (
            "egress_pinned" in host
            and host["egress_pinned"] is not True
            and host["egress_pinned"] is not False
        )
        if pinned:
            pinned_count += 1
        if pinned_malformed:
            pinned_malformed_count += 1
        report = {
            "instance_id": str(host["instance_id"]),
            "applied_mode": host.get("egress_applied_mode", "") or "",
            "applied_version": (
                str(applied_version) if applied_version is not None else ""
            ),
            "applied_sha256": host.get("egress_applied_sha256", "") or "",
            "applied_at": host.get("egress_applied_at", "") or "",
            "reconcile_error": host.get("egress_reconcile_error", "") or "",
            "pinned": pinned,
            "pinned_malformed": pinned_malformed,
            "policy_source": host.get("egress_policy_source", "") or "",
            # 这台【自己那行】的期望态。portal 的"指定机器"作用域要用它做表单初值 ——
            # 期望态是全量替换,拿 fleet 单例的值去填一台走 host 行的机器,提交就会把它
            # 自己的放行规则整套换成别人的。null 与 "" 必须分开:null = 这行没有期望态
            # (跟 fleet 走),"" = 有期望态且明确没有放行规则。
            "desired_mode": host.get("egress_mode") or None,
            "desired_deny_rfc1918": (
                bool(host["egress_deny_rfc1918"])
                if "egress_deny_rfc1918" in host
                else None
            ),
            "desired_extra_allow": (
                str(host.get("egress_extra_allow") or "")
                if host.get("egress_mode")
                else None
            ),
            "converged": False,
        }
        if pinned_malformed:
            reason = "pinned_malformed"
        elif pinned:
            if fleet_policy.get("egress_mode") == "off":
                # 单向语义:机队熔断 off 在写入时刻赢,即使 host 行仍保留 pin。
                report["converged"], _detail = _classify_host(host, desired)
            elif host.get("egress_mode"):
                report["converged"] = _classify_against_own_row(host)
            elif fleet_policy:
                # pin 没有 host policy 时不能制造"无期望态",与 host-agent 选择一致。
                report["converged"], _detail = _classify_host(host, desired)
            # pinned 机器即使已按自己的期望态收敛,也始终是 fleet 非均一离群。
            reason = "pinned_exempt"
        elif host.get("egress_policy_source") == "host" and host.get("egress_mode"):
            # 走 host 行的机器(定向灰度)必须按【它自己那份期望态】判,不能拿单例比。
            #
            # 为什么:host 行没有 policy_version 字段,host-agent 于是上报
            # egress_desired_at 这个 ISO 串当版本(host-agent.py:4082-4084),而
            # _classify_host 拿它去比单例的毫秒整数 policy_version(:307)——
            # 两个字符串永不相等 → 每一台定向灰度的机器都恒报 version_mismatch。
            # 真机同一张表里两种类型并存已确证:走单例的报 1787591853330,
            # 走 host 行的报 2026-08-24T17:23:53Z。
            # 本轮的 _classify_pinned_host 只给 pinned 机器绕开了这条,
            # 普通定向灰度(不带 pin)仍中招 —— 而灰度正是切换流程的第一步。
            report["converged"] = _classify_against_own_row(host)
            reason = "targeted_desired_state"
        elif fleet_policy:
            report["converged"], reason = _classify_host(host, desired)
        else:
            reason = (
                "never_reported"
                if not host.get("egress_applied_mode")
                else "fleet_policy_not_set"
            )
        if reason:
            outliers.append(
                {"instance_id": report["instance_id"], "reason": reason}
            )
        hosts.append(report)

    # 指纹【不参与】收敛判定,只作为信息性分组上报。
    #
    # 为什么不跨 host 比 sha:host 侧的 allow 洞 IP 是各自 `socket.gethostbyname(LITELLM_HOST)`
    # 解析出来的单个 A 记录(见 _ON_HOST 内联 python)。LiteLLM 走 internal ALB 时 A 记录是
    # 多条且轮转,不同 host 合法地解析到不同 IP → `-d <ip> --dport <port> -j ACCEPT` 规则文本
    # 不同 → `iptables -S | sha256sum` 天然不同。此时机队【完全正常收敛】,若把"跨机 sha 相等"
    # 当收敛判据就会把它整队报成未收敛,运维在真实事故里会被带偏。
    # (`ADR-guest-egress-default-deny-whitelist` §6 已把"LiteLLM 在会变 IP 的 internal ALB"
    #  列为已知失败模式;#575 记录客户环境实测该域名解析出 2 个 A 记录。)
    #
    # 指纹的正确用途是【同一台机器与自己的上一次比】—— 那是 host-agent 侧的 drift 判据
    # (_egress_sha_drifted),回答"装完之后有没有被带外动过"。policy_version 回答"是哪一版"。
    # 两者职责不能互换。因此这里只按 sha 分组呈现,让运维自己判断分歧是合法差异还是真漂移。
    fingerprint_groups = {}
    for host in hosts:
        sha = host["applied_sha256"] or "(not reported)"
        fingerprint_groups.setdefault(sha, []).append(host["instance_id"])
    rules_sha256, rules_sha256_reason = _summarize_rules_sha256(
        host["applied_sha256"] for host in hosts
    )

    total = len(hosts)
    converged = sum(1 for host in hosts if host["converged"])
    returned_hosts = hosts
    instance_ids_not_found = []
    if instance_ids_filter is not None:
        known_ids = {host["instance_id"] for host in hosts}
        instance_ids_not_found = [
            iid for iid in instance_ids_filter if iid not in known_ids
        ]
        selected_ids = set(instance_ids_filter)
        returned_hosts = [
            host for host in hosts if host["instance_id"] in selected_ids
        ]
    hosts_truncated = limit is not None and len(returned_hosts) > limit
    if limit is not None:
        returned_hosts = returned_hosts[:limit]

    body = {
        "desired": desired,
        "total": total,
        "converged": converged,
        # 收敛 = mode 一致 + policy_version 一致 + 无 reconcile 错误。刻意不含 sha。
        "fully_converged": bool(total) and converged == total,
        "fleet_uniform": (
            pinned_count == 0
            and pinned_malformed_count == 0
            and not outliers
        ),
        "pinned_count": pinned_count,
        "pinned_malformed_count": pinned_malformed_count,
        # 全机队指纹唯一时给出该值,否则 null —— 仅供取证,不代表未收敛。
        "rules_sha256": rules_sha256,
        "rules_sha256_reason": rules_sha256_reason,
        "fingerprint_groups": {
            sha: sorted(ids) for sha, ids in sorted(fingerprint_groups.items())
        },
        "hosts": returned_hosts,
        "hosts_returned": len(returned_hosts),
        "hosts_truncated": hosts_truncated,
        "limit": limit,
        "instance_ids_filter": instance_ids_filter,
        "outliers": outliers,
    }
    if instance_ids_filter is not None:
        body["instance_ids_not_found"] = instance_ids_not_found
    if not fleet_policy:
        body["message"] = "fleet policy has never been set"
    return _resp(200, body)


def _parse_host_output(text):
    parsed = {
        "apply_exit": None,
        "rules_sha256": None,
        "pinned_skip": False,
        "pin_check": None,
    }
    for line in (text or "").splitlines():
        if line.startswith("APPLY_EXIT="):
            try:
                parsed["apply_exit"] = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("RULES_SHA256="):
            parsed["rules_sha256"] = line.split("=", 1)[1].strip()
        elif line.startswith("PINNED_SKIP="):
            parsed["pinned_skip"] = (
                line.split("=", 1)[1].strip() == "1"
            )
        elif line.startswith("PIN_CHECK="):
            parsed["pin_check"] = (
                line.split("=", 1)[1].strip().split(None, 1)[0]
            )
    return parsed


def _collect(command_id, budget_sec, expected_count=0):
    """用 list_command_invocations 回收逐机 apply_exit + rules_sha256(兼容 Targets 与
    InstanceIds 两种扇出)。MEDIUM fix:①全量分页 NextToken(不再只看前 50);②早退用
    expected_count gate —— 仅当【已收齐 ≥ 期望数】且全终态才 break,避免 Targets 下 host 增量
    注册时"先到的几台已终态"就误判 consistent/漏报(expected_count=0 时退化为只靠 deadline)。"""
    deadline = time.time() + budget_sec
    results = {}
    terminal = {"Success", "Failed", "Cancelled", "TimedOut", "Undeliverable", "Terminated"}
    while time.time() < deadline:
        time.sleep(4)
        token = None
        try:
            while True:
                kwargs = {"CommandId": command_id, "Details": True, "MaxResults": 50}
                if token:
                    kwargs["NextToken"] = token
                resp = clients.ssm.list_command_invocations(**kwargs)
                for inv in resp.get("CommandInvocations", []):
                    out = ""
                    for pl in inv.get("CommandPlugins", []):
                        out += pl.get("Output", "") or ""
                    parsed = _parse_host_output(out)
                    result = {
                        "instance_id": inv.get("InstanceId"),
                        "ssm_status": inv.get("Status"),
                    }
                    result.update(parsed)
                    results[inv.get("InstanceId")] = result
                token = resp.get("NextToken")
                if not token:
                    break
        except Exception as e:  # noqa: BLE001 — 继续重试是对的,但原因必须可见
            # 原来这里是裸 `continue`:一次权限缺失或 API 抖动会让本函数安静地轮询到
            # deadline,调用方只看到"没有结果",排查时完全没有线索(#603 定位这条 bug
            # 时全靠手工重放 SSM 才发现真因)。控制流不变,只把原因打出来。
            print(f"_collect list_command_invocations failed (will retry): {e}")
            continue
        all_terminal = bool(results) and all(r["ssm_status"] in terminal for r in results.values())
        if all_terminal and len(results) >= max(expected_count, 1):
            break
    return list(results.values())


def _apply_timeout():
    return int(os.environ.get("EGRESS_APPLY_TIMEOUT", "90"))


def _dispatch_apply(
    mode, deny_rfc1918, extra_allow, enforce_pin, instance_ids, use_tag
):
    """渲染 _ON_HOST 并用一条 SSM send_command 下发。返回 (command_id, error_str)。

    从 fleet_egress 原地提取,逐字节保留原 kwargs —— 回滚路径要按"每台机器各自的旧期望态"
    分组下发(一组一条 command),不能共用一份渲染后的脚本,所以必须可复用。
    """
    script = (
        _ON_HOST.replace("__MODE__", mode)
        .replace("__ENFORCE_PIN__", "true" if enforce_pin else "false")
        .replace("__DENY_RFC1918__", "true" if deny_rfc1918 else "false")
        .replace("__REGION__", _REGION)
        .replace("__EXTRA_ALLOW__", extra_allow or "")
    )
    b64 = base64.b64encode(script.encode()).decode()
    timeout = _apply_timeout()
    send_kwargs = dict(
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                f"echo {b64} | base64 -d > /tmp/oc_egress_admin.sh",
                "sudo bash /tmp/oc_egress_admin.sh",
            ],
            "executionTimeout": [str(timeout)],
        },
        # SSM 拒绝小于 30 的 TimeoutSeconds(boto 参数校验层直接拒,见本文件 rollback 侧探针
        # 那处硬编码 30 的注释)。而 timeout 来自 EGRESS_APPLY_TIMEOUT,运维可以配任意值 ——
        # 配到 20 以下时这里就会派生出非法参数,`POST /hosts/egress` 每次调用都 502,端点直接
        # 不可用(真机实测:EGRESS_APPLY_TIMEOUT=1 -> TimeoutSeconds=11 -> 502)。
        # 夹紧放在发车处而不是 _apply_timeout():两个数含义不同 —— timeout 是我们自己的回收
        # 预算(_collect 轮询这么久,真正的上界是 API GW 的 29s 窗口),TimeoutSeconds 是 SSM
        # 放弃投递的上限。短回收预算是合法的运维选择,非法的 SSM 参数不是。
        TimeoutSeconds=max(30, timeout + 10),
        MaxConcurrency="100%",
        MaxErrors="100%",
    )
    # #566 follow-up — 全机队走 tag Targets(无 50 上限、跳过终止中实例、含未来新机);
    # 显式小 target 列表才用 InstanceIds(≤50,验收/定点场景)。
    if use_tag:
        send_kwargs["Targets"] = [{"Key": "tag:Role", "Values": [HOST_TAG_ROLE]}]
    else:
        send_kwargs["InstanceIds"] = list(instance_ids)
    try:
        resp = clients.ssm.send_command(**send_kwargs)
        return resp["Command"]["CommandId"], None
    except Exception as e:  # noqa: BLE001
        print(f"fleet-egress SSM send error: {e}")
        return None, str(e)


def fleet_egress(body=None, event=None):
    """POST /hosts/egress — 一次修改全部(或指定)host 的 guest 出网防火墙。

    Body: {"mode":"deny"|"off", "targets":"all"|["i-..."], "deny_rfc1918":false,
           "wait":true}

    Admin-only(最高爆炸半径:动全机队网络隔离)。wait=true 时轮询到终态并返回逐机
    apply_exit + rules_sha256 + consistent(默认 true,给验收取证);wait=false 走
    fire-and-forget 只返 command_id(生产大机队用,避免撑爆 29s API-GW 窗口)。

    allow 的作用域下界由 EGRESS_EXTRA_ALLOW_MIN_PREFIX 决定(默认 /24,最宽 /16,#668)。
    比默认更宽的洞会被放行,但响应体的 extra_allow_scope 显式回报这次开了多宽 ——
    风险由调用方承担,API 不让它只能从一个 202 里推。下发前想先看判定走
    POST /hosts/egress/allow/validate(只读 dry-run)。
    """
    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403, {"error": "forbidden: fleet egress admin requires admin", "required": "admin"}
        )
    body = json.loads(body) if isinstance(body, str) else (body or {})
    mode = body.get("mode")
    if not isinstance(mode, str) or mode.strip().lower() not in VALID_MODES:
        return _resp(400, {"error": f"mode must be one of {list(VALID_MODES)}"})
    mode = mode.strip().lower()
    pinned_present = "pinned" in body
    pinned = body.get("pinned")
    if pinned_present and pinned is not True and pinned is not False:
        return _resp(400, {"error": "pinned must be a boolean when provided"})
    deny_rfc1918 = bool(body.get("deny_rfc1918", False))
    # #566 follow-up — 默认 fire-and-forget。wait=true 会在 API GW 29s 硬窗口内轮询所有
    # host 的 SSM 到终态,机队一多(实测 ~9 台起)必 504;300 机队更是必然。默认 false 立即
    # 返 202 + command_id,逐机收敛由 GET /hosts(host-agent 上报)或 get-command-invocation
    # 异步回读。wait=true 仅供小机队/验收显式取逐机 rules_sha256。
    wait = bool(body.get("wait", False))
    targets = body.get("targets", "all")
    # 爆炸半径不能从 truthiness 推。旧写法是
    #     is_all = not (isinstance(targets, list) and targets)
    # 即【任何不是非空 list 的值】都当"全机队":一个漏写方括号的
    # {"mode":"off","targets":"i-0abc123"} 本意是改一台,实际会写 fleet 单例 + 按
    # tag(Role=metal-host, MaxConcurrency=100%)把 teardown 以 root 扇给每台 host,
    # 把全部 microVM 的默认拒绝链拆掉并持久化。dict / null / [] 同理。
    # 同一函数里 mode 是枚举严校验、pinned 是 identity 严校验,所以这是遗漏不是有意宽容;
    # 202 body 里那个 informational 的 "targeting" 字段是在 SSM 已下发【之后】才返回的,
    # 拦不住任何事。契约(docstring)只有两种形状,其余一律 400 —— 不猜调用方意图。
    _TARGETS_ERR = 'targets must be "all" or a non-empty list of instance id strings'
    if isinstance(targets, str):
        if targets.strip().lower() != "all":
            return _resp(400, {"error": _TARGETS_ERR, "got": targets[:64]})
        is_all = True
    elif isinstance(targets, list):
        if not targets or not all(isinstance(t, str) and t.strip() for t in targets):
            return _resp(400, {"error": _TARGETS_ERR, "got": f"list[{len(targets)}]"})
        is_all = False
    else:
        return _resp(400, {"error": _TARGETS_ERR, "got": type(targets).__name__})
    if is_all and pinned is True:
        return _resp(
            400,
            {
                "error": (
                    "pinned=true is not allowed for the entire fleet; "
                    "pinning every host would disable future fleet-wide updates"
                )
            },
        )
    # #566 follow-up — 运维加放行端口:allow=[{proto,dport,dst}](仅 deny 模式有意义)。
    #
    # 期望态是【全量替换】,不是增量:省略 allow → _build_extra_allow(None) 返 ""
    # → _write_fleet_policy 无条件 SET egress_extra_allow = "" → 运维此前加的洞
    # 被静默清空。「我只想改 mode / deny_rfc1918」这个念头,用一次朴素 POST 表达出来
    # 就顺带删掉了全部额外放行洞,而 API 返 202、收敛报告随后报 fully_converged。
    # (LiteLLM 的洞不受影响 —— 它由每台 host 从自己的 /etc/platform.env 派生,
    #  见 _ON_HOST 内联 python 读 EGRESS_LLM_CIDR/LITELLM_HOST,不经 API body。)
    #
    # 这里不改语义(改成增量会让"清空洞"变得无法表达,且破坏既有调用方),
    # 只把后果显式化:省略 allow 而当前期望态确有洞时,响应体报 extra_allow_cleared,
    # 让调用方看得见这次动作删了什么。
    allow_present = "allow" in body
    extra_allow, err = _build_extra_allow(body.get("allow"))
    if err:
        return _resp(400, {"error": err})
    extra_allow_scope = (
        _summarize_allow_scope(extra_allow, _extra_allow_min_prefix())
        if extra_allow
        else None
    )
    extra_allow_scope_warning = ""
    if extra_allow_scope and extra_allow_scope["below_default_floor"]:
        extra_allow_scope_warning = (
            "; NOTE this publish opened an allow hole wider than the /24 default "
            f"floor (widest /{extra_allow_scope['widest_prefix']}); the caller "
            "owns that blast radius (#668)"
        )
    # 按【本次实际会写哪一层】去读旧值,不能一律读单例:
    #   is_all  → 写的是单例,读单例(且必须在 _write_fleet_policy 之前读,否则读到新值、
    #             提示恒空,是个假绿);
    #   定向    → 写的是那几台的 host 行,读那几台的 host 行。一律读单例的话,定向路径上
    #             这个字段会【同时漏报和误报】:真正被清空的 host 行没人报,而报出来的是
    #             这次根本没被写的 fleet 单例的旧值。
    # 对 pinned host 尤其要紧 —— 它收敛用的就是自己那行(host-agent.py 把胜出 policy 的
    # egress_extra_allow 喂给 chain.sh),一次不带 allow 的定向 deny 会静默拿掉它全部洞。
    # 枚举与"改之前"的读取必须都排在任何写入之前 —— 版本记录里的 before 是回滚的唯一依据,
    # 读到新值等于把回滚点写成了当前值(假绿:回滚看起来成功、实际什么都没变)。
    #
    # 这次读【不再只是提示】。它同时是回滚锚点的输入,所以读失败不能当无事发生:
    # 读不到 fleet 单例时,一台其实跟着 fleet 跑 deny 的机器会被记成 source=none,
    # 将来那次回滚就会把它写成 mode=off —— 拆掉它全部租户的白名单链,而且没人会知道
    # 是这次读失败造成的。故普通路径 fail-closed 返 503(期望态与内核都还没动,重试安全)。
    #
    # 唯一例外仍是 mode=off 这条 break-glass:熔断不能被一次 DDB 抖动挡住(§2.3 单向语义)。
    # 那条路径继续下发,但把"读不到"如实记进快照(source=unknown),让将来的回滚对这些机器
    # 直接 409 拒绝,而不是拿一份猜出来的旧态去写。
    break_glass = mode == "off"
    fleet_readable = True
    force_tag_dispatch = False
    try:
        fleet_before = _read_fleet_policy() or {}
        if is_all:
            instance_ids, host_rows = _enumerate_hosts(targets, include_rows=True)
        else:
            instance_ids = _enumerate_hosts(targets)
            host_rows = _read_host_rows_strict(instance_ids)
    except Exception as e:  # noqa: BLE001 — 回滚锚点不可信时必须 fail-closed
        print(f"egress pre-change read failed: {e}")
        if not break_glass:
            return _resp(
                503,
                {
                    "error": f"cannot read current desired state, refusing to dispatch: {e}",
                    "hint": "读不到改动前的期望态就记不下可信的回滚点;期望态与内核均未改动,重试是安全的",
                },
            )
        fleet_readable = False
        fleet_before = {}
        host_rows = []
        if is_all:
            # 全量的枚举是一次 scan,DDB 挂了它也会挂。但全量下发【本来就走 tag Targets】,
            # 不需要 instance_ids —— 所以这里留空并置 force_tag_dispatch,让下面那条
            # "no active hosts" 早退不要把熔断吞成 no-op。
            instance_ids = []
            force_tag_dispatch = True
        else:
            # 定向的 instance_ids 就是调用方给的那份,_enumerate_hosts 对非空 list
            # 不读 DDB,不会因为这次故障而缺。
            instance_ids = [str(t) for t in targets]

    extra_allow_cleared = {}
    if not allow_present and fleet_readable:
        if is_all:
            prior = fleet_before.get("egress_extra_allow", "") or ""
            if prior:
                extra_allow_cleared["__fleet_egress_policy__"] = prior
        else:
            for row in host_rows:
                prior = row.get("egress_extra_allow", "") or ""
                if prior:
                    extra_allow_cleared[str(row.get("instance_id"))] = prior

    revision, rev_error = _record_revision(
        body.get("revision_name"),
        ident,
        is_all,
        instance_ids,
        host_rows,
        fleet_before if fleet_readable else None,
        {
            "mode": mode,
            "deny_rfc1918": deny_rfc1918,
            "extra_allow": extra_allow,
            "pinned": pinned if pinned_present else None,
        },
        kind="apply",
    )
    if rev_error:
        return _resp(rev_error[0], rev_error[1])

    if is_all:
        _write_fleet_policy(mode, deny_rfc1918, extra_allow)

    unpinned_count = 0
    unpin_failed = []
    if is_all and pinned is False:
        unpinned_count, unpin_failed, _unpin_skipped = _remove_egress_pins(host_rows)
    # pin 的强制只在 is_all 且 mode != off 时开启(见下面 __ENFORCE_PIN__ 的替换条件),
    # 因为机队熔断必须穿透 pin(单向语义)。所以 pinned_skipped 也必须跟着 mode 走 ——
    # 否则熔断的响应会列出 pinned 机器说「跳过了」,而它们实际执行了 teardown、链已被拆。
    # 字段与事实相反,恰好出现在唯一那条 break-glass 路径上,会让运维以为豁免机器还留着链。
    pin_enforced = bool(is_all and mode != "off")
    # 定向路径此前 host_rows 恒为空 → pinned_hosts 恒为 []。本轮为了拍 before 快照把
    # 定向的 host 行也读出来了,这里显式收窄回全量路径,免得响应里凭空多出
    # pinned_torn_down(定向下发根本不 teardown pinned 机器,那个字段名会误导运维)。
    pin_report_rows = host_rows if is_all else []
    pinned_hosts = sorted(
        str(host["instance_id"])
        for host in pin_report_rows
        if host.get("status") in ("active", "idle")
        and host.get("egress_pinned") is True
        and pinned is not False
    )
    # 真正被本次下发跳过的;熔断时为空(它们没被跳过,是被拆了链)。
    pinned_skipped = pinned_hosts if pin_enforced else []
    # force_tag_dispatch:break-glass 且机队枚举读不到时,instance_ids 必然为空,但那次
    # 熔断必须照发(走 tag Targets)。不加这个条件的话,DDB 抖动会把唯一的 break-glass
    # 路径静默变成 200 "no active hosts" —— API 说成功、链一条没拆。
    if not instance_ids and not force_tag_dispatch:
        return _resp(
            200,
            {
                "mode": mode,
                "hosts": 0,
                "revision": revision,
                "pin_enforced": pin_enforced,
                "pinned_torn_down": [] if pin_enforced else pinned_hosts,
                "pinned_skipped": pinned_skipped,
                "unpinned_count": unpinned_count,
                "unpin_failed": unpin_failed or None,
                "extra_allow_cleared": extra_allow_cleared or None,
                "desired_state_incomplete": False,
                "message": "no active hosts",
            },
        )

    # 1) 声明式期望态写 DDB(扛 host 重建 → host-agent reconcile 会重新 apply)
    #
    # targets=all 时【只写单例、不逐机写 host mode】。批量 unpin 只 REMOVE pin 属性。理由:
    #   · 扛重建/新机收敛本来就是单例的职责(#577 增量-1),全量再逐机写 host 行已冗余。
    #   · 显式 egress_pinned=true 的 host 由 host-agent 按 host 行收敛;全量不能重写其 mode。
    #   · 定向 targets=[...] 仍写 host 行,这是豁免与灰度的唯一载体。
    if is_all:
        desired_written = 0
    else:
        desired_written = _write_desired_state(
            instance_ids,
            mode,
            deny_rfc1918,
            extra_allow,
            pinned=pinned if pinned_present else None,
        )
    # 全量只写 fleet 单例,desired_written 按设计恒为 0;只有定向逐机写才可按数量判缺口。
    desired_state_incomplete = (
        not is_all and desired_written != len(instance_ids)
    )
    desired_state_warning = ""
    if desired_state_incomplete:
        desired_state_warning = (
            f"; WARNING desired state written for only {desired_written}/"
            f"{len(instance_ids)} hosts — the un-written hosts will have their "
            "chain reverted by the next host-agent reconcile"
        )

    # 2) live-apply via 一条 SSM send_command 扇出全机队(fleet_power 同款)
    timeout = _apply_timeout()
    command_id, send_error = _dispatch_apply(
        mode,
        deny_rfc1918,
        extra_allow,
        enforce_pin=bool(is_all and mode != "off"),
        instance_ids=instance_ids,
        use_tag=bool(is_all or force_tag_dispatch),
    )
    if send_error:
        return _resp(502, {"error": f"failed to dispatch fleet-egress: {send_error}"})

    # 变更审计由 handler 对 mutating verb 统一落账(handler.py dispatch 后段);此处不再重复。
    if not wait:
        return _resp(
            202,
            {
                "mode": mode,
                "revision": revision,
                "command_id": command_id,
                "host_count": len(instance_ids),
                "desired_state_written": desired_written,
                "desired_state_incomplete": desired_state_incomplete,
                "desired_state_scope": (
                    "fleet-singleton" if is_all else "per-instance"
                ),
                "pin_enforced": pin_enforced,
                "pinned_torn_down": [] if pin_enforced else pinned_hosts,
                "pinned_skipped": pinned_skipped,
                "unpinned_count": unpinned_count,
                "unpin_failed": unpin_failed or None,
                "extra_allow": extra_allow or None,
                "extra_allow_cleared": extra_allow_cleared or None,
                "extra_allow_scope": extra_allow_scope or None,
                "targeting": "tag:Role" if is_all else "instance-ids",
                "message": (
                    "dispatched; poll get-command-invocation or GET /hosts for convergence"
                    + desired_state_warning
                    + extra_allow_scope_warning
                ),
            },
        )

    hosts = _collect(command_id, timeout, expected_count=len(instance_ids))
    expected_host_count = len(instance_ids)
    if is_all:
        # 全量的 DDB 枚举只是一份快照:可能含已终止或 SSM 不受管的机器,也可能漏掉刚注册
        # 但会被 tag 扇出命中的新机。拿它判"应该回几台"会制造恒 207,所以只报数字不算差集。
        missing_hosts = None
        collection_incomplete = False
    else:
        collected_ids = {str(h.get("instance_id")) for h in hosts}
        missing_hosts = sorted(
            str(iid) for iid in instance_ids if str(iid) not in collected_ids
        )
        collection_incomplete = bool(missing_hosts)
    rules_sha256, rules_sha256_reason = _summarize_rules_sha256(
        h.get("rules_sha256") for h in hosts if not h.get("pinned_skip")
    )
    command_ok = bool(hosts) and all(
        h["apply_exit"] == 0 and h["ssm_status"] == "Success" for h in hosts
    )
    pin_check_unavailable = sorted(
        str(h["instance_id"])
        for h in hosts
        if h.get("pin_check") == "unavailable"
    )
    all_ok = command_ok and not pin_check_unavailable and not collection_incomplete
    # 没有任何真实指纹不是“一致”;缺失证据不能被包装成没有问题。
    consistent = all_ok and rules_sha256 is not None
    collection_warning = ""
    if collection_incomplete:
        collection_warning = (
            f"; WARNING {len(missing_hosts)} of {expected_host_count} targeted hosts "
            f"returned no invocation within the {timeout}s window — their chain state "
            "is unknown"
        )
    return _resp(
        200 if all_ok else 207,
        {
            "ok": all_ok,
            "mode": mode,
            "revision": revision,
            "command_id": command_id,
            "host_count": len(hosts),
            "expected_host_count": expected_host_count,
            "missing_hosts": missing_hosts,
            "collection_incomplete": collection_incomplete,
            "desired_state_written": desired_written,
            "desired_state_incomplete": desired_state_incomplete,
            "desired_state_scope": "fleet-singleton" if is_all else "per-instance",
            "pin_enforced": pin_enforced,
            "pinned_torn_down": [] if pin_enforced else pinned_hosts,
            "pinned_skipped": pinned_skipped,
            "unpinned_count": unpinned_count,
            "unpin_failed": unpin_failed or None,
            "pin_check_unavailable": pin_check_unavailable,
            "extra_allow": extra_allow or None,
            "extra_allow_cleared": extra_allow_cleared or None,
            "extra_allow_scope": extra_allow_scope or None,
            "consistent": consistent,
            "rules_sha256": rules_sha256,
            "rules_sha256_reason": rules_sha256_reason,
            "hosts": hosts,
            "message": (
                "apply completed" if all_ok else "apply completed with errors"
            ) + collection_warning + desired_state_warning + extra_allow_scope_warning,
        },
    )


def _recorded_extra_allow_has_valid_shape(raw):
    """历史值只做语法体检;不拿今天的红线策略否决当时合法的回滚点。"""
    if raw is None or raw == "":
        return True
    if not isinstance(raw, str):
        return False
    for token in raw.split(","):
        parts = token.split(":", 2)
        if len(parts) != 3:
            return False
        proto, dport, dst = parts
        if proto not in ("tcp", "udp") or not dport.isdigit() or not dst:
            return False
        if not 1 <= int(dport) <= 65535:
            return False
    return True


def fleet_egress_rollback(body=None, event=None):
    """POST /hosts/egress/rollback — 按命名版本里的逐机 before 恢复并重新下发。"""
    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403, {"error": "forbidden: fleet egress admin requires admin", "required": "admin"}
        )
    try:
        body = json.loads(body) if isinstance(body, str) else (body or {})
    except (TypeError, ValueError):
        return _resp(400, {"error": "body must be a JSON object"})
    if not isinstance(body, dict):
        return _resp(400, {"error": "body must be a JSON object"})

    if "targets" not in body:
        return _resp(
            400,
            {
                "error": (
                    'targets is required on rollback; pass "all" explicitly '
                    "to roll back every recorded host"
                )
            },
        )
    # 形状门排在最前:与 fleet_egress 同源、同措辞。爆炸半径的判定不许被别的错误抢先,
    # 也不许依赖"恰好有一个合法版本名"才能被触发。
    targets = body["targets"]
    _TARGETS_ERR = 'targets must be "all" or a non-empty list of instance id strings'
    if isinstance(targets, str):
        if targets.strip().lower() != "all":
            return _resp(400, {"error": _TARGETS_ERR, "got": targets[:64]})
        is_all = True
    elif isinstance(targets, list):
        if not targets or not all(isinstance(t, str) and t.strip() for t in targets):
            return _resp(400, {"error": _TARGETS_ERR, "got": f"list[{len(targets)}]"})
        is_all = False
    else:
        return _resp(400, {"error": _TARGETS_ERR, "got": type(targets).__name__})

    restore_fleet_singleton = body.get("restore_fleet_singleton", False)
    if restore_fleet_singleton is not True and restore_fleet_singleton is not False:
        return _resp(
            400,
            {
                "error": (
                    "restore_fleet_singleton must be a boolean when provided"
                )
            },
        )

    requested_revision = body.get("revision")
    if not isinstance(requested_revision, str) or not requested_revision.strip():
        return _resp(
            400, {"error": "revision is required and must be a string"}
        )
    try:
        revisions = _list_revisions()
    except Exception as e:  # noqa: BLE001 — 历史读失败不能伪装成"版本不存在"
        print(f"egress rollback revision list failed: {e}")
        return _resp(
            503,
            {
                "error": f"cannot read revisions, refusing rollback: {e}",
                "hint": "读不到历史版本时不能安全判断回滚目标;未写期望态、未下发",
            },
        )
    revision = next(
        (
            item
            for item in revisions
            if item.get("name") == requested_revision
        ),
        None,
    )
    if revision is None:
        return _resp(
            404,
            {
                "error": f"revision not found: {requested_revision!r}",
                "known_revisions": [item.get("name") for item in revisions[:20]],
            },
        )

    raw_before_incomplete = revision.get("before_incomplete")
    if (
        raw_before_incomplete is not False
        and raw_before_incomplete is not None
        and bool(raw_before_incomplete)
    ):
        return _resp(
            409,
            {
                "error": (
                    f"revision {requested_revision} is not a usable rollback anchor: "
                    "its pre-change desired state could not be read when it was recorded"
                ),
                "hint": (
                    "那次是 break-glass 熔断,DDB 读不到改动前的期望态仍放行了下发。"
                    "这条记录只是变更账,拿它回滚等于按一份没读到的旧态去写。"
                    "改用它之前的某个完整版本,或手工确认后用定向 POST /hosts/egress。"
                ),
                "before_incomplete_raw_type": (
                    revision.get("before_incomplete_raw_type")
                    or type(raw_before_incomplete).__name__
                ),
            },
        )

    before = revision.get("before") or {}
    fleet_snapshot_present = "__fleet__" in before
    if restore_fleet_singleton and not fleet_snapshot_present:
        return _resp(
            400,
            {
                "error": (
                    f"revision {requested_revision} records no fleet singleton "
                    "state to restore"
                )
            },
        )
    fleet_prior = {}
    fleet_restore_mode = None
    if restore_fleet_singleton:
        fleet_prior = before.get("__fleet__") or {}
        fleet_restore_mode = fleet_prior.get("mode")
        if (
            fleet_restore_mode not in VALID_MODES
            and fleet_restore_mode not in ("", None)
        ):
            return _resp(
                409,
                {
                    "error": (
                        f"revision {requested_revision} has invalid fleet mode: "
                        f"{fleet_restore_mode!r}"
                    )
                },
            )

    # 两条路径只能从同一份候选集选 host。伪键或不像 EC2 instance id 的脏键一律不能
    # 进入 DDB 写与 SSM InstanceIds;定向输入另行区分为 rejected_targets。
    recorded_keys = list(dict.fromkeys(str(iid) for iid in before))
    recorded_ids = [
        iid
        for iid in recorded_keys
        if _is_dispatchable_host_id(iid)
    ]
    recorded_id_set = set(recorded_ids)
    rejected_recorded = [
        iid for iid in recorded_keys if iid not in recorded_id_set
    ]
    if is_all:
        instance_ids = recorded_ids
        not_in_revision = []
        rejected_targets = rejected_recorded
    else:
        requested_ids = list(
            dict.fromkeys(str(iid).strip() for iid in targets)
        )
        rejected_targets = [
            iid
            for iid in requested_ids
            if not _is_dispatchable_host_id(iid)
        ]
        valid_requested_ids = [
            iid for iid in requested_ids if iid not in rejected_targets
        ]
        instance_ids = [
            iid for iid in valid_requested_ids if iid in recorded_id_set
        ]
        not_in_revision = [
            iid for iid in valid_requested_ids if iid not in recorded_id_set
        ]

    if not instance_ids:
        if is_all:
            return _resp(
                409,
                {
                    "error": (
                        f"revision {requested_revision} records no per-host "
                        "state to roll back"
                    ),
                    "hint": (
                        "版本行存在但 before 里没有任何可用的 host 条目 —— "
                        '这是残缺记录,不是"无需回滚"'
                    ),
                    "not_in_revision": not_in_revision,
                    "rejected_targets": rejected_targets,
                },
            )
        return _resp(
            409,
            {
                "error": (
                    f"revision {requested_revision} records no matching "
                    "per-host state to roll back"
                ),
                "not_in_revision": not_in_revision,
                "rejected_targets": rejected_targets,
            },
        )

    restore_rows = []
    restored_as = {}
    grouped = {}
    malformed_recorded_state = []
    for iid in instance_ids:
        prior = before.get(iid) or {}
        raw_extra_allow = prior.get("extra_allow")
        if not _recorded_extra_allow_has_valid_shape(raw_extra_allow):
            malformed_recorded_state.append(
                {"instance_id": iid, "field": "extra_allow"}
            )
            continue
        source = prior.get("source")
        if source == "none":
            mode = "off"
            deny_rfc1918 = False
            extra_allow = ""
            # 当时【压根没有期望态】。写 off 是为了复现当时的观测态(host-agent 把缺失
            # 强制成 off),并用 restored_as 暴露“从无期望态变成显式 host 行”的来源变化。
            # 这不代表永久脱离 fleet:未 pin 时下一次全量仍会靠更新时间戳覆盖它。
            restored_as[iid] = "host-row-from-unset"
        elif source in ("host", "fleet"):
            mode = prior.get("mode")
            if mode not in VALID_MODES:
                return _resp(
                    409,
                    {
                        "error": (
                            f"revision {requested_revision} has invalid mode for "
                            f"{iid}: {mode!r}"
                        )
                    },
                )
            deny_rfc1918 = bool(prior.get("deny_rfc1918", False))
            extra_allow = raw_extra_allow or ""
            if source == "fleet":
                # fleet 单例可能早已前进;先把历史实际值恢复成 host 行。后续能否继续
                # 压过 fleet 仍取决于 pin,不能从“已有显式 host 行”推断持久性。
                restored_as[iid] = "host-row"
        else:
            return _resp(
                409,
                {
                    "error": (
                        f"revision {requested_revision} has invalid source for "
                        f"{iid}: {source!r}"
                    )
                },
            )
        pinned = prior.get("pinned") is True
        restore_rows.append(
            {
                "instance_id": iid,
                "mode": mode,
                "deny_rfc1918": deny_rfc1918,
                "extra_allow": extra_allow,
                "pinned": pinned,
            }
        )
        grouped.setdefault((mode, deny_rfc1918, extra_allow), []).append(iid)

    if not restore_rows:
        return _resp(
            409,
            {
                "error": (
                    f"revision {requested_revision} records no usable per-host "
                    "state to roll back"
                ),
                "not_in_revision": not_in_revision,
                "rejected_targets": rejected_targets,
                "malformed_recorded_state": malformed_recorded_state,
            },
        )

    # 回滚也必须先拍"回滚之前"。强一致读取任一失败就停,否则会把读失败误判成跟 fleet。
    try:
        host_rows = _read_host_rows_strict(instance_ids)
        fleet_before = _read_fleet_policy() or {}
    except Exception as e:  # noqa: BLE001 — 回滚点不完整时必须 fail-closed
        print(f"egress rollback current-state read failed: {e}")
        return _resp(
            503,
            {
                "error": f"cannot read current desired state, refusing rollback: {e}",
                "hint": "未记录回滚点、未写期望态、未下发",
            },
        )

    rollback_revision, rev_error = _record_revision(
        body.get("revision_name"),
        ident,
        is_all,
        instance_ids,
        host_rows,
        fleet_before,
        {
            # 回滚可能含多种 mode;用动作名避免把"含 off"误当成可跳过版本记录的熔断。
            "mode": "rollback",
            "deny_rfc1918": False,
            "extra_allow": "",
            "pinned": None,
        },
        kind="rollback",
        include_fleet_snapshot=restore_fleet_singleton,
    )
    if rev_error:
        return _resp(rev_error[0], rev_error[1])

    # 这个字段通常是 bool;仅“恢复到从未设置过”时返回 "cleared",让调用方能区分
    # 清空期望态与恢复了某个具体 mode。这是本接口唯一允许的 bool|string 扩展。
    fleet_singleton_restored = False
    if restore_fleet_singleton:
        if fleet_restore_mode in VALID_MODES:
            _write_fleet_policy(
                fleet_restore_mode,
                bool(fleet_prior.get("deny_rfc1918", False)),
                fleet_prior.get("extra_allow") or "",
            )
            fleet_singleton_restored = True
        else:
            _clear_fleet_policy()
            fleet_singleton_restored = "cleared"

    desired_written = 0
    desired_state_failed = []
    for row in restore_rows:
        written, failed_ids = _write_desired_state_detailed(
            [row["instance_id"]],
            row["mode"],
            row["deny_rfc1918"],
            row["extra_allow"],
            pinned=row["pinned"],
        )
        desired_written += written
        desired_state_failed.extend(failed_ids)

    # 没落下期望态的 host 即使 live-apply 成功也会被下一轮 reconcile 翻回去,不制造这个窗口。
    failed_id_set = set(desired_state_failed)
    # 回滚持久性由 egress_pinned 决定,不是由“有没有显式 host 行”或 restored_as 决定。
    # 判据源头在 host-agent.py 的 _egress_policy_source;改这里或判据时必须同步核对另一处。
    durability = {
        "survives_fleet_all_except_off": [
            row["instance_id"]
            for row in restore_rows
            if row["pinned"] and row["instance_id"] not in failed_id_set
        ],
        "overridden_by_next_fleet_all": [
            row["instance_id"]
            for row in restore_rows
            if not row["pinned"] and row["instance_id"] not in failed_id_set
        ],
        "fleet_off_overrides_pinned": True,
    }
    grouped = {
        key: [iid for iid in grouped_ids if iid not in failed_id_set]
        for key, grouped_ids in grouped.items()
    }

    dispatches = []
    failed_dispatches = []
    for (mode, deny_rfc1918, extra_allow), grouped_ids in grouped.items():
        for offset in range(0, len(grouped_ids), 50):
            chunk = grouped_ids[offset : offset + 50]
            command_id, send_error = _dispatch_apply(
                mode,
                deny_rfc1918,
                extra_allow,
                enforce_pin=False,
                instance_ids=chunk,
                use_tag=False,
            )
            dispatch = {
                "command_id": command_id,
                "mode": mode,
                "deny_rfc1918": deny_rfc1918,
                "extra_allow": extra_allow,
                "instance_ids": chunk,
                "count": len(chunk),
            }
            if send_error:
                dispatch["error"] = send_error
                failed_dispatches.append(dispatch)
            else:
                dispatches.append(dispatch)

    if failed_dispatches:
        message = "rollback partially dispatched"
    elif dispatches:
        message = "rollback dispatched"
    else:
        message = "rollback not dispatched"
    # 期望态是 best-effort 逐台写。旧行为在部分失败时链已改、期望态没改 —— host-agent
    # 下一轮 reconcile 会按旧期望态把链改回去;只给两个数字让调用方自己比,等于没报。
    # 现在写失败的 host 不再下发,并把具体 id 显式返回,避免制造注定被撤销的内核窗口。
    desired_state_incomplete = desired_written != len(instance_ids)
    if desired_state_incomplete:
        message += (
            f"; WARNING desired state written for only {desired_written}/"
            f"{len(instance_ids)} hosts — the un-written hosts will have their "
            "chain reverted by the next host-agent reconcile"
        )
    if (
        durability["survives_fleet_all_except_off"]
        or durability["overridden_by_next_fleet_all"]
    ):
        message += (
            "; rollback durability follows egress_pinned, not explicit host rows: "
            'unpinned restored hosts remain effective only until the next targets="all" '
            "publish refreshes the fleet singleton updated_at and overrides them; "
            "explicitly pin hosts that must retain the rollback across later fleet "
            "publishes; fleet off still overrides pinned hosts"
        )
    fleet_singleton_not_restored = (
        fleet_snapshot_present and not restore_fleet_singleton
    )
    if fleet_singleton_not_restored:
        message += (
            "; fleet singleton was not restored: 新建/重建的 host 仍会收敛到当前单例值,"
            "如需一并回滚请带 restore_fleet_singleton=true"
        )
    response = {
        "revision": requested_revision,
        "rollback_revision": rollback_revision,
        "dispatches": dispatches,
        "dispatch_count": len(dispatches),
        "failed_dispatches": failed_dispatches,
        "host_count": len(instance_ids),
        "desired_state_written": desired_written,
        "desired_state_failed": desired_state_failed,
        "not_in_revision": not_in_revision,
        "rejected_targets": rejected_targets,
        "malformed_recorded_state": malformed_recorded_state,
        "restored_as": restored_as,
        "durability": durability,
        "desired_state_incomplete": desired_state_incomplete,
        "fleet_singleton_restored": fleet_singleton_restored,
        "fleet_singleton_not_restored": fleet_singleton_not_restored,
        "message": message,
    }
    # wait=true 时每组各等一次 _collect;组数一多就会超过 API GW 的 29s 硬窗口,
    # 那时状态已经改完、只是响应丢了。先把这件事说清楚,免得被当成"回滚失败"。
    if bool(body.get("wait", False)) and len(dispatches) > 1:
        response["wait_warning"] = (
            f"{len(dispatches)} dispatch groups are polled sequentially; this can "
            "exceed the 29s API Gateway window. A 504 here does NOT mean the "
            "rollback failed — poll GET /hosts/egress instead."
        )

    if not bool(body.get("wait", False)):
        return _resp(207 if failed_dispatches else 202, response)

    hosts = []
    for dispatch in dispatches:
        results = _collect(
            dispatch["command_id"],
            _apply_timeout(),
            expected_count=dispatch["count"],
        )
        for result in results:
            result["command_id"] = dispatch["command_id"]
        hosts.extend(results)
    expected_results = sum(dispatch["count"] for dispatch in dispatches)
    all_ok = (
        bool(expected_results)
        and not failed_dispatches
        and len(hosts) == expected_results
        and all(
            host.get("apply_exit") == 0
            and host.get("ssm_status") == "Success"
            for host in hosts
        )
    )
    response["ok"] = all_ok
    response["hosts"] = hosts
    return _resp(200 if all_ok else 207, response)


_CHAIN_COUNTER_KEYS = frozenset({
    "FWD_TAP_TOTAL", "FWD_TAP_DROP", "FWD_TAP_ACCEPT", "FWD_TAP_IMDS_DROP",
    "FWD_TAP_REDLINE_DROP", "IN_TAP_TOTAL", "IN_TAP_DROP",
    "IN_8899", "IN_9090", "IN_9100", "IN_22", "TAP_DEVICES",
    "FWD_JUMP_POS", "FWD_ANCHOR_POS", "FWD_PRECEDING_ACCEPTS",
})


def _parse_chain_output(text):
    """解析只读 SSM 证据;缺任一标记都视为读取不完整,不能推断链不存在。"""
    chain_present = None
    rules_sha256 = None
    forward_jumps = None
    rules = []
    per_tap_sample = []
    precede_sample = []
    section = None
    saw_rules = False
    saw_per_tap = False
    saw_precede = False
    saw_end = False
    counters = {}
    for line in (text or "").splitlines():
        if line.startswith("CHAIN_PRESENT="):
            raw = line.split("=", 1)[1].strip()
            if raw in ("yes", "no"):
                chain_present = raw == "yes"
        elif line.startswith("RULES_SHA256="):
            rules_sha256 = line.split("=", 1)[1].strip()
        elif line.startswith("FORWARD_JUMPS="):
            try:
                forward_jumps = int(line.split("=", 1)[1].strip())
            except ValueError:
                forward_jumps = None
        elif "=" in line and line.split("=", 1)[0] in _CHAIN_COUNTER_KEYS:
            key, raw = line.split("=", 1)
            try:
                counters[key.lower()] = int(raw.strip())
            except ValueError:
                counters[key.lower()] = None
        elif line == "---RULES---":
            section = "rules"
            saw_rules = True
        elif line == "---PERTAP---":
            section = "per-tap"
            saw_per_tap = True
        elif line == "---PRECEDE---":
            section = "precede"
            saw_precede = True
        elif line == "---END---":
            # 收尾哨兵:它在 = 输出完整。SSM 对大输出会截断,截断后 per-tap 段看起来"就那么
            # 几条",与"这台真的只有几条 tap 规则"不可区分 —— 必须能分开。
            section = None
            saw_end = True
        elif section == "rules":
            if line.strip():
                rules.append(line)
        elif section == "per-tap":
            if line.strip():
                per_tap_sample.append(line)
        elif section == "precede":
            if line.strip():
                precede_sample.append(line)
    precedence_counters = (
        "FWD_JUMP_POS",
        "FWD_ANCHOR_POS",
        "FWD_PRECEDING_ACCEPTS",
    )
    if (
        chain_present is None
        or rules_sha256 is None
        or forward_jumps is None
        or any(counters.get(key.lower()) is None for key in precedence_counters)
        or not saw_rules
        or not saw_per_tap
        or not saw_precede
        or not saw_end
    ):
        missing = [
            name for name, ok in (
                ("CHAIN_PRESENT", chain_present is not None),
                ("RULES_SHA256", rules_sha256 is not None),
                ("FORWARD_JUMPS", forward_jumps is not None),
                *(
                    (key, counters.get(key.lower()) is not None)
                    for key in precedence_counters
                ),
                ("---RULES---", saw_rules),
                ("---PERTAP---", saw_per_tap),
                ("---PRECEDE---", saw_precede),
                ("---END---", saw_end),
            ) if not ok
        ]
        return None, (
            "kernel read output was incomplete or malformed; missing: "
            + ", ".join(missing)
        )
    return (
        {
            "chain_present": chain_present,
            "rules_sha256": rules_sha256,
            "forward_jumps": forward_jumps,
            "rules": rules,
            "per_tap_sample": per_tap_sample,
            "precede_sample": precede_sample,
            # 三层各自的计数分开报。合成一个"健康"数会让"只有一半在守"看着像全都在守 ——
            # 真机实测过一台:FORWARD 侧红线端口 DROP 是 0、INPUT 侧是 218,也就是
            # guest 打【别的】host 的 8899 没人挡(G4 未回补到存量 tap)。
            "layers": counters,
        },
        None,
    )


def fleet_egress_chain(event=None):
    """GET /hosts/egress/chain — 从单台 host 内核回读当前真实链证据。"""
    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403, {"error": "forbidden: fleet egress admin requires admin", "required": "admin"}
        )
    query = (event or {}).get("queryStringParameters") or {}
    instance_id = query.get("instance_id")
    if (
        not isinstance(instance_id, str)
        or not instance_id.strip()
        or not instance_id.strip().startswith("i-")
    ):
        return _resp(
            400, {"error": "instance_id is required and must start with 'i-'"}
        )
    instance_id = instance_id.strip()
    read_at = _now()

    def inconclusive(error):
        return _resp(
            200,
            {
                "instance_id": instance_id,
                "source": "kernel",
                "read_at": read_at,
                "verdict": "INCONCLUSIVE",
                "error": str(error),
            },
        )

    # 全程用 awk 而不是 grep:grep 无匹配退 1,而它一旦是脚本【最后一条】命令,整条 SSM
    # 就报 Failed/RC=1 —— 真机实测正是这样:现网 17 台全 mode=off、一条 tap 规则都没有,
    # 于是这条只读查询每次都被判成失败、返 INCONCLUSIVE。awk 无匹配也退 0。
    # 末尾的 ---END--- 既让脚本以一条必然成功的命令收尾,又充当"输出没被截断"的证据。
    script = r"""echo "CHAIN_PRESENT=$(iptables -S OPENCLAW-EGRESS >/dev/null 2>&1 && echo yes || echo no)"
R=$(iptables -S OPENCLAW-EGRESS 2>/dev/null)
# 链不存在时 `iptables -S | sha256sum` 会算【空串】的指纹(e3b0c442…)——
# 三元组的第三条判据于是恒为真,页面上还会显示一个看着像真指纹的值。无规则就输出空。
if [ -n "$R" ]; then echo "RULES_SHA256=$(printf '%s\n' "$R" | sha256sum | cut -d' ' -f1)"; else echo "RULES_SHA256="; fi
echo "FORWARD_JUMPS=$(iptables -S FORWARD 2>/dev/null | awk '/-j OPENCLAW-EGRESS/{n++} END{print n+0}')"
# per-tap 黑名单层的计数。正则不能写成 ^-A FORWARD -i tap —— DROP 规则的实际形状是
# `-A FORWARD -d <dst> -i tapX ...`(-d 在 -i 之前),那样只会匹配到兜底 ACCEPT,
# 于是页面上「per-tap 黑名单层」看着一条 DROP 都没有(真机上它有 218 条)。
iptables -S FORWARD 2>/dev/null | awk '
  /^-A FORWARD .*-i tap/ {
    t++
    if (/-j DROP/) d++
    if (/-j ACCEPT/) a++
    if (/169[.]254[.]169[.]254/) imds++
    if (/--dport 8899/ || /--dport 9090/ || /--dport 9100/ || /--dport 22 /) rl++
  }
  END{ printf "FWD_TAP_TOTAL=%d\nFWD_TAP_DROP=%d\nFWD_TAP_ACCEPT=%d\nFWD_TAP_IMDS_DROP=%d\nFWD_TAP_REDLINE_DROP=%d\n",
       t+0, d+0, a+0, imds+0, rl+0 }'
# INPUT 方向单独数:红线管理端口(8899/9090/9100/22)的 DROP 装在 INPUT 上,
# 它只保护 guest 所在的那一台 host;打别的 host 走 FORWARD(那是 G4 的职责)。
# 两个方向必须分开报 —— 合成一个数会让「只有一半在守」看起来像全都在守。
iptables -S INPUT 2>/dev/null | awk '
  /^-A INPUT .*-i tap/ {
    t++
    if (/-j DROP/) d++
    if (/--dport 8899/) p1++
    if (/--dport 9090/) p2++
    if (/--dport 9100/) p3++
    if (/--dport 22 /) p4++
  }
  END{ printf "IN_TAP_TOTAL=%d\nIN_TAP_DROP=%d\nIN_8899=%d\nIN_9090=%d\nIN_9100=%d\nIN_22=%d\n",
       t+0, d+0, p1+0, p2+0, p3+0, p4+0 }'
echo "TAP_DEVICES=$(ip -o link show 2>/dev/null | awk '/tap/{n++} END{print n+0}')"
# 以下三类判据逐条照抄 oc-egress-chain.sh 的 verify_forward_precedence:
# 只豁免严格 RELATED,ESTABLISHED 锚点;安全 target 仅 DROP/REJECT/LOG 与在役链 jump。
# RETURN、自定义链、goto 和未知 target 一律 fail-closed,否则 apply 与回读会互相矛盾。
iptables -S FORWARD 2>/dev/null | awk '
  function option_value(line, option, fields, count, i) {
    count = split(line, fields, /[[:space:]]+/)
    for (i = 1; i < count; i++) {
      if (fields[i] == option) return fields[i + 1]
    }
    return ""
  }
  function has_ctstate(ctstates, wanted, states, count, i) {
    count = split(ctstates, states, ",")
    for (i = 1; i <= count; i++) {
      if (states[i] == wanted) return 1
    }
    return 0
  }
  function only_established_states(ctstates, states, count, i) {
    count = split(ctstates, states, ",")
    if (count != 2) return 0
    for (i = 1; i <= count; i++) {
      if (states[i] != "RELATED" && states[i] != "ESTABLISHED") return 0
    }
    return 1
  }
  function rule_target(line, fields, count, i) {
    count = split(line, fields, /[[:space:]]+/)
    for (i = 1; i < count; i++) {
      if (fields[i] == "-j") return fields[i + 1]
      if (fields[i] == "-g") return "goto:" fields[i + 1]
    }
    return ""
  }
  function is_conntrack_anchor(line, ctstates) {
    ctstates = option_value(line, "--ctstate")
    return line ~ /-m conntrack/ &&
           ctstates != "" &&
           has_ctstate(ctstates, "RELATED") &&
           has_ctstate(ctstates, "ESTABLISHED") &&
           !has_ctstate(ctstates, "NEW") &&
           only_established_states(ctstates) &&
           rule_target(line) == "ACCEPT"
  }
  function is_safe_target(target) {
    return target == "DROP" || target == "REJECT" || target == "LOG"
  }
  function may_hit_guest(line) {
    return line !~ / -i / || line ~ / -i tap/
  }
  /^-A FORWARD / { rules[++total] = $0 }
  END {
    for (i = 1; i <= total; i++) {
      if (!jump && rules[i] ~ / -j OPENCLAW-EGRESS$/) jump = i
      if (!anchor && is_conntrack_anchor(rules[i])) {
        anchor = i
      }
    }
    if (jump) {
      for (i = 1; i < jump; i++) {
        target = rule_target(rules[i])
        if (may_hit_guest(rules[i]) &&
            !is_conntrack_anchor(rules[i]) &&
            !is_safe_target(target)) {
          offenders++
          if (samples < 5) sample[++samples] = rules[i]
        }
      }
    } else {
      # -1 表示未能评估,与 0(评估过且无 offender)是两个不同事实,禁止合并;
      # 语义同源于 oc-egress-chain.sh:392 的 jump 缺失即 fail-closed。
      offenders = -1
    }
    printf "FWD_JUMP_POS=%d\nFWD_ANCHOR_POS=%d\nFWD_PRECEDING_ACCEPTS=%d\n",
           jump+0, anchor+0, offenders+0
    print "---PRECEDE---"
    for (i = 1; i <= samples; i++) print sample[i]
  }'
echo '---RULES---'
if [ -n "$R" ]; then printf '%s\n' "$R"; fi
echo '---PERTAP---'
# DROP 优先展示:兜底 ACCEPT 每 tap 一条,不筛的话前 40 条全被它占满,而要看的恰是 DROP。
iptables -S FORWARD 2>/dev/null | awk '/^-A FORWARD .*-i tap/ && /-j DROP/{print; if (++n>=30) exit}'
iptables -S FORWARD 2>/dev/null | awk '/^-A FORWARD .*-i tap/ && /-j ACCEPT/{print; if (++n>=10) exit}'
echo '---END---'"""
    try:
        sent = clients.ssm.send_command(
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [script],
                "executionTimeout": ["15"],
            },
            # SSM 的 TimeoutSeconds 下限是 30(真机实测:传 25 会被 boto 参数校验直接拒,
            # 于是每次回读都返 INCONCLUSIVE)。这个值只是 SSM 侧放弃投递的上限,与我们
            # 自己 _collect 的 18s 预算无关 —— API GW 的 29s 窗口由后者守。
            TimeoutSeconds=30,
            MaxConcurrency="1",
            MaxErrors="0",
            InstanceIds=[instance_id],
        )
        command_id = sent["Command"]["CommandId"]
    except Exception as e:  # noqa: BLE001 — 读不到必须显式回 INCONCLUSIVE
        print(f"fleet-egress kernel read dispatch failed {instance_id}: {e}")
        return inconclusive(f"failed to dispatch kernel read: {e}")

    # 单机回读【不走 _collect】。两个原因:
    #   1. _collect 是给扇出用的,它靠 ssm:ListCommandInvocations —— 而真机实测该 action
    #      此前没授(role 只有 GetCommandInvocation),于是它每轮都 AccessDenied、被裸
    #      except 吞掉、轮到 deadline 返空列表,本接口每次都报 INCONCLUSIVE。
    #   2. 就算权限补齐了,一台机器用 get_command_invocation 直接轮询更短、更少一跳,
    #      且这个 action 本来就已经授权 —— 不必让一个只读查询依赖一条待部署的 IAM 改动。
    # 预算必须【严格小于】API GW 的 29s 硬窗口:留不出返回时间的话,离线实例会把这次
    # 只读查询变成 504,而 504 与"链被拆了"在调用方看来不可区分 —— 那正是本接口要分开的两件事。
    deadline = time.time() + 18
    invocation = None
    last_status = None
    terminal = {"Success", "Failed", "Cancelled", "TimedOut", "Undeliverable", "Terminated"}
    while time.time() < deadline:
        time.sleep(2)
        try:
            invocation = clients.ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except Exception as e:  # noqa: BLE001 — 读不到必须显式回 INCONCLUSIVE,不能当"没装链"
            # InvocationDoesNotExist 在刚下发的一两秒内是正常的,继续轮询;
            # 其它错误(权限/限流)也继续,但把原因留在日志里,最后由 INCONCLUSIVE 带出。
            last_status = f"{type(e).__name__}: {e}"
            invocation = None
            continue
        last_status = invocation.get("Status")
        if last_status in terminal:
            break
    if invocation is None or last_status not in terminal:
        print(f"fleet-egress kernel read not terminal {instance_id}: {last_status}")
        return inconclusive(
            f"kernel read did not reach a terminal state in time (last: {last_status})"
        )
    if (
        invocation.get("Status") != "Success"
        or invocation.get("ResponseCode") != 0
    ):
        error = invocation.get("StandardErrorContent") or (
            f"kernel read command returned status={invocation.get('Status')} "
            f"response_code={invocation.get('ResponseCode')}"
        )
        return inconclusive(error)

    parsed, parse_error = _parse_chain_output(
        invocation.get("StandardOutputContent", "")
    )
    if parse_error:
        return inconclusive(parse_error)
    # FORWARD_JUMPS 的既有正则不锚定行尾,会把 -new/-old 残留跳转也数进去;
    # fwd_jump_pos 是唯一能证明“有跳转指向在役链”的量,判定侧必须同时收紧。
    chain_ready = (
        parsed["chain_present"]
        and parsed["forward_jumps"] > 0
        and parsed["layers"].get("fwd_jump_pos") not in (None, 0)
        and bool(parsed["rules_sha256"])
    )
    preceding = parsed["layers"].get("fwd_preceding_accepts")
    precedence_evaluated = isinstance(preceding, int) and preceding >= 0
    effective = (
        chain_ready
        and precedence_evaluated
        and preceding == 0
    )
    if effective:
        verdict = "EFFECTIVE"
    elif chain_ready and precedence_evaluated:
        # SHORT_CIRCUITED 必须与 NOT_EFFECTIVE 分开:前者说明链齐备但被前置不安全
        # target 改变控制流,重新 apply 不一定能修;后者才是链/跳转/规则缺失。
        verdict = "SHORT_CIRCUITED"
        sample = parsed["precede_sample"]
        reason = (
            "链装着、规则对着、指纹也对,但排在跳转前面的不安全规则会先改变"
            "guest 流量控制流,所以包可能不进这条链。"
        )
        if sample:
            reason += "offending rules:\n" + "\n".join(sample)
        else:
            reason += "样本缺失(可能被 SSM 截断)。"
        parsed["short_circuit_reason"] = reason
    else:
        verdict = "NOT_EFFECTIVE"
    parsed.update(
        {
            "instance_id": instance_id,
            "source": "kernel",
            "read_at": read_at,
            "verdict": verdict,
        }
    )
    return _resp(200, parsed)
