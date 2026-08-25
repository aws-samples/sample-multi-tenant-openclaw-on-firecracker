# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/egress_admin_service — 控制面 fleet guest 出网防火墙运维 API(ADR 拆分项②)。

实现 `POST /hosts/egress`:运维者一次调用即修改全部(或指定)host 的 guest 出网
default-deny 白名单链 `OPENCLAW-EGRESS`,拿到 API 响应 + 逐机一致性证据。

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


def _build_extra_allow(allow):
    """把 API 的 allow=[{proto,dport,dst}] 校验并编码成 EGRESS_EXTRA_ALLOW 串。

    护栏:proto∈tcp/udp;dport 1-65535;dst 必填且为 IP/CIDR(拒绝无目的地放行);
    拒绝对 IMDS 开洞。返回 (str, error)。
    """
    import ipaddress as _ip
    if allow is None:
        return "", None
    if not isinstance(allow, list):
        return "", "allow must be a list of {proto,dport,dst}"
    toks = []
    for i, e in enumerate(allow):
        if not isinstance(e, dict):
            return "", f"allow[{i}] must be an object"
        proto = str(e.get("proto", "")).strip().lower()
        if proto not in ("tcp", "udp"):
            return "", f"allow[{i}].proto must be tcp|udp"
        try:
            dport = int(e.get("dport"))
        except (TypeError, ValueError):
            return "", f"allow[{i}].dport must be an integer"
        if not 1 <= dport <= 65535:
            return "", f"allow[{i}].dport out of range"
        dst = str(e.get("dst", "")).strip()
        if not dst:
            return "", f"allow[{i}].dst is required (IP/CIDR)"
        try:
            net = _ip.ip_network(dst, strict=False)
        except ValueError:
            return "", f"allow[{i}].dst must be IP/CIDR"
        # HIGH fix — 链是 IPv4-only iptables;放过 IPv6 dst 会让 host 侧 apply 失败、整链换入
        # 中止,且毒 token 被 DDB 持久化 → reconcile 永久卡 / fresh-host 静默 fail-open。拒 IPv6。
        if net.version != 4:
            return "", f"allow[{i}].dst must be IPv4 (chain is IPv4-only)"
        if _ip.ip_address(_IMDS) in net:
            return "", f"allow[{i}] must not open IMDS ({_IMDS})"
        toks.append(f"{proto}:{dport}:{net}")
    return ",".join(toks), None

# 在每台 host 上跑的一段(读该 host 真实 /etc/platform.env 派生 LiteLLM allow 洞 + apply/
# teardown + 回读 OPENCLAW-EGRESS 的 sha256 供逐机一致性核对)。__MODE__ 由调用方替换。
# 空 host(无 guest)无 conntrack anchor → 补幂等前置;真实 host 有 guest 时前置是 no-op。
_ON_HOST = r'''
set -u
MODE="__MODE__"; DENY_RFC1918="__DENY_RFC1918__"
B=$(grep -oE 'openclaw-assets-[0-9]+' /etc/platform.env 2>/dev/null | head -1)
R=$(grep -E '^OC_REGION=' /etc/platform.env | cut -d= -f2 | tr -d '"'); [ -z "$R" ] && R=__REGION__
aws s3 cp "s3://$B/deployment/scripts/oc-egress-chain.sh" /home/ubuntu/oc-egress-chain.sh --region "$R" --quiet
aws s3 cp "s3://$B/deployment/scripts/oc-egress-sim.py"  /home/ubuntu/oc-egress-sim.py  --region "$R" --quiet
chmod +x /home/ubuntu/oc-egress-chain.sh
if [ "$MODE" = "off" ]; then
  VPC_CIDR="10.0.0.0/8" TAP_IFACE="tap+" bash /home/ubuntu/oc-egress-chain.sh teardown 2>&1 || true
  echo "APPLY_EXIT=0"
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
u = urlparse(raw if "://" in raw else "//" + raw, scheme=""); host = u.hostname or ""; port = u.port
sch = (u.scheme or "").lower()
if not port:
    port = 443 if sch == "https" else (80 if sch == "http" else 4000)
ip = ""
try:
    ip = socket.gethostbyname(host) if host else ""
except Exception:
    ip = ""
inv = False
try:
    inv = ip and ipaddress.ip_address(ip) in ipaddress.ip_network(vpc, strict=False)
except Exception:
    inv = False
print(f"{vpc}|{ip if inv else ''}|{port}")
PY
)
VPC="${DERIVED%%|*}"; REST="${DERIVED#*|}"; LLM_IP="${REST%%|*}"; LLM_PORT="${REST##*|}"
iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
ip link show tap0 >/dev/null 2>&1 || { ip tuntap add tap0 mode tap; ip link set tap0 up; }
VPC_CIDR="$VPC" LITELLM_HOST="$LLM_IP" LITELLM_PORT="$LLM_PORT" SPIRE_SERVER="" TAP_IFACE="tap+" DENY_RFC1918="$DENY_RFC1918" EGRESS_EXTRA_ALLOW="__EXTRA_ALLOW__" \
  bash /home/ubuntu/oc-egress-chain.sh apply
echo "APPLY_EXIT=$?"
echo "RULES_SHA256=$(iptables -S OPENCLAW-EGRESS 2>/dev/null | sha256sum | cut -d' ' -f1)"
'''


def _enumerate_hosts(targets):
    """指定 targets(instance_id 列表)则用之;否则强一致扫全部 active/idle host。"""
    if isinstance(targets, list) and targets:
        return [str(t) for t in targets]
    hosts = ddb_scan.scan_all(
        clients.hosts_table,
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    )
    return [h["instance_id"] for h in hosts if h.get("instance_id")]


def _write_desired_state(instance_ids, mode, deny_rfc1918, extra_allow=""):
    """声明式期望态写 DDB openclaw-hosts(taint 模式,扛 host 重建)。best-effort。

    同时持久化 egress_extra_allow,让 host-agent reconcile 在 reboot/重建后连同额外放行洞
    一起收敛(否则重启只恢复 deny 基线、丢掉运维加的端口)。
    """
    now = _now()
    written = 0
    for iid in instance_ids:
        try:
            clients.hosts_table.update_item(
                Key={"instance_id": iid},
                UpdateExpression=(
                    "SET egress_mode = :m, egress_deny_rfc1918 = :r, "
                    "egress_extra_allow = :x, egress_desired_at = :t"
                ),
                # 镜像 taint 先例:只写已存在的 host 行,绝不 upsert 幽灵行。
                ConditionExpression="attribute_exists(instance_id)",
                ExpressionAttributeValues={
                    ":m": mode,
                    ":r": bool(deny_rfc1918),
                    ":x": extra_allow or "",
                    ":t": now,
                },
            )
            written += 1
        except Exception as e:  # noqa: BLE001 — 期望态 best-effort,不阻断 live-apply
            print(f"egress desired-state write failed {iid}: {e}")
    return written


def _parse_host_output(text):
    exit_code, sha = None, None
    for line in (text or "").splitlines():
        if line.startswith("APPLY_EXIT="):
            try:
                exit_code = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("RULES_SHA256="):
            sha = line.split("=", 1)[1].strip()
    return exit_code, sha


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
                    ec, sha = _parse_host_output(out)
                    results[inv.get("InstanceId")] = {
                        "instance_id": inv.get("InstanceId"),
                        "ssm_status": inv.get("Status"),
                        "apply_exit": ec,
                        "rules_sha256": sha,
                    }
                token = resp.get("NextToken")
                if not token:
                    break
        except Exception:  # noqa: BLE001
            continue
        all_terminal = bool(results) and all(r["ssm_status"] in terminal for r in results.values())
        if all_terminal and len(results) >= max(expected_count, 1):
            break
    return list(results.values())


def fleet_egress(body=None, event=None):
    """POST /hosts/egress — 一次修改全部(或指定)host 的 guest 出网防火墙。

    Body: {"mode":"deny"|"off", "targets":"all"|["i-..."], "deny_rfc1918":false,
           "wait":true}

    Admin-only(最高爆炸半径:动全机队网络隔离)。wait=true 时轮询到终态并返回逐机
    apply_exit + rules_sha256 + consistent(默认 true,给验收取证);wait=false 走
    fire-and-forget 只返 command_id(生产大机队用,避免撑爆 29s API-GW 窗口)。
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
    deny_rfc1918 = bool(body.get("deny_rfc1918", False))
    # #566 follow-up — 默认 fire-and-forget。wait=true 会在 API GW 29s 硬窗口内轮询所有
    # host 的 SSM 到终态,机队一多(实测 ~9 台起)必 504;300 机队更是必然。默认 false 立即
    # 返 202 + command_id,逐机收敛由 GET /hosts(host-agent 上报)或 get-command-invocation
    # 异步回读。wait=true 仅供小机队/验收显式取逐机 rules_sha256。
    wait = bool(body.get("wait", False))
    targets = body.get("targets", "all")
    is_all = not (isinstance(targets, list) and targets)
    # #566 follow-up — 运维加放行端口:allow=[{proto,dport,dst}](仅 deny 模式有意义)。
    extra_allow, err = _build_extra_allow(body.get("allow"))
    if err:
        return _resp(400, {"error": err})

    instance_ids = _enumerate_hosts(targets)
    if not instance_ids:
        return _resp(200, {"mode": mode, "hosts": 0, "message": "no active hosts"})

    # 1) 声明式期望态写 DDB(扛 host 重建 → host-agent reconcile 会重新 apply)
    desired_written = _write_desired_state(instance_ids, mode, deny_rfc1918, extra_allow)

    # 2) live-apply via 一条 SSM send_command 扇出全机队(fleet_power 同款)
    script = (
        _ON_HOST.replace("__MODE__", mode)
        .replace("__DENY_RFC1918__", "true" if deny_rfc1918 else "false")
        .replace("__REGION__", _REGION)
        .replace("__EXTRA_ALLOW__", extra_allow)
    )
    b64 = base64.b64encode(script.encode()).decode()
    timeout = int(os.environ.get("EGRESS_APPLY_TIMEOUT", "90"))
    send_kwargs = dict(
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                f"echo {b64} | base64 -d > /tmp/oc_egress_admin.sh",
                "sudo bash /tmp/oc_egress_admin.sh",
            ],
            "executionTimeout": [str(timeout)],
        },
        TimeoutSeconds=timeout + 10,
        MaxConcurrency="100%",
        MaxErrors="100%",
    )
    # #566 follow-up — 全机队走 tag Targets(无 50 上限、跳过终止中实例、含未来新机);
    # 显式小 target 列表才用 InstanceIds(≤50,验收/定点场景)。
    if is_all:
        send_kwargs["Targets"] = [{"Key": "tag:Role", "Values": [HOST_TAG_ROLE]}]
    else:
        send_kwargs["InstanceIds"] = instance_ids
    try:
        resp = clients.ssm.send_command(**send_kwargs)
        command_id = resp["Command"]["CommandId"]
    except Exception as e:  # noqa: BLE001
        print(f"fleet-egress SSM send error: {e}")
        return _resp(502, {"error": f"failed to dispatch fleet-egress: {e}"})

    # 变更审计由 handler 对 mutating verb 统一落账(handler.py dispatch 后段);此处不再重复。
    if not wait:
        return _resp(
            202,
            {
                "mode": mode,
                "command_id": command_id,
                "host_count": len(instance_ids),
                "desired_state_written": desired_written,
                "extra_allow": extra_allow or None,
                "targeting": "tag:Role" if is_all else "instance-ids",
                "message": "dispatched; poll get-command-invocation or GET /hosts for convergence",
            },
        )

    hosts = _collect(command_id, timeout, expected_count=len(instance_ids))
    shas = {h["rules_sha256"] for h in hosts}
    all_ok = bool(hosts) and all(
        h["apply_exit"] == 0 and h["ssm_status"] == "Success" for h in hosts
    )
    consistent = all_ok and len(shas) == 1 and None not in shas
    return _resp(
        200 if all_ok else 207,
        {
            "ok": all_ok,
            "mode": mode,
            "command_id": command_id,
            "host_count": len(hosts),
            "desired_state_written": desired_written,
            "extra_allow": extra_allow or None,
            "consistent": consistent,
            "rules_sha256": next(iter(shas)) if len(shas) == 1 else None,
            "hosts": hosts,
        },
    )
