#!/usr/bin/env python3
"""spire-join-broker —— host 侧 per-microVM join-token 代发器(零侵入落地)

它解决什么
----------
SPIRE agent 在 guest 里要一个一次性 join token 才能完成 node attestation。上游
《launch-vm.sh SPIRE 改造说明》把 token 经 MMDS 注入,代价是必须改 `launch-vm.sh`
(pre-boot `PUT /mmds/config` + `PUT /mmds`)—— 那是全租户启动路径上风险最高的文件,
而且 MMDS 只能 pre-boot 配,任何漏配都要重启 VM 才能补。

本 broker 换一条**已经存在**的通道:每台 VM 独占的 /30 tap 点对点链路。guest 用它的
默认网关(= 自己 tap 的 host 端 IP)访问 broker,broker 用【TCP 连接的目的 IP + 源 IP】
把请求钉到唯一一台 VM,再向 Entry Registrar / 本地 spire-server 换一次性 join token
回传。`launch-vm.sh`、`build-rootfs.sh`、`host-agent.py` 一行都不用改。

判据(本仓一手代码,2026-08-17 读取)
-----------------------------------
* 每 VM 一条 /30:HOST_TAP_IP=.+1 / GUEST_IP=.+2 —— deploy/userdata/launch-vm.sh:718-721
* vm.json 落 tenant_id/vm_num/guest_ip           —— deploy/userdata/launch-vm.sh:724-727
* guest→host INPUT 默认 ACCEPT,只 DROP 8899/9090/22/9100
                                                 —— deploy/userdata/launch-vm.sh:1864-1868
* guest→host tap IP:53(dnsmasq)已有同型先例    —— deploy/userdata/launch-vm.sh:1895-1896
* 生产 launch-vm.sh 至今零 MMDS 配置(grep `170.2` 零命中)
                                                 —— engineering/security/SPIRE-方案采用-生产改造清单.md

身份判定(为什么这条通道能钉到"哪台 VM")
----------------------------------------
1. **目的 IP**:每条 /30 的 host 端 IP 只属于一台 VM。broker 取 accept 后 socket 的
   `getsockname()`(内核给的目的地址,客户端无法伪造)→ 唯一 VM。
2. **源 IP**:必须等于同一条 /30 的 guest 端 IP。
3. **反向路径过滤**:内核丢掉"从 tapA 进来却声称是 tapB 源地址"的包 —— 这才是源 IP
   不可伪造的依据,不是"约定"。但**生效的那个机制必须选对**:
   * `net.ipv4.conf.*.rp_filter` 的生效值是 `max(conf/all, conf/<iface>)`(kernel.org
     ip-sysctl 原文),**不是** per-tap 那个文件的值。ClawPool host 的
     `/etc/sysctl.d/10-network-security.conf` 把 `all` 设成 `2`(loose),于是把 tap 设成
     `1` 之后生效值仍是 `max(2,1)=2` —— **loose 挡不住伪造**(2026-08-18 真机 netns 实测)。
     而把 `all` 改成 `1` 会连带把主 ENI 切 strict,不能由本 kit 单方面决定。
   * 所以真正落地的是 **`iptables -t raw -m rpfilter --invert -j DROP`**:它在规则级做
     strict 反向路径检查,**不看 `conf/all` 这个全局旋钮**,`-i tap+` 通配还能自动覆盖
     之后新建的 tap(零改 launch-vm.sh 的前提下,per-tap 记账式规则对新 VM 会失效)。
   broker 默认 `--rp-filter-policy enforce`:**两种机制都不在**就拒发(见 `spoof_guard`)。
4. **一次性**:同一 VM 同一次开机(fc.sock 的 mtime 作 boot marker)只发一枚 token。

诚实边界(不夸大)
------------------
* Firecracker 无 vTPM(r8g.metal 实测 nitroTpm:unsupported):SVID 私钥能被 SSH 进 VM 的
  合法租户 cat 走。本 broker 不改变这条,它只把"权威签发 + 短命 + 可批量撤销"的身份
  发到每台 VM 手里。定位与 ADR-spire-per-microvm-poc 第 6 节一致。
* broker 的信任根 = host 自己的网络拓扑(谁的 tap / 谁的 /30),与 MMDS 的信任根
  ("host 写、guest 只读")同级,都不是 guest 侧密码学证明。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8877  # host 侧空闲端口(8192/8250/8576/8601/8640/8789/8790/8899/9090/9100 已被占)
DEFAULT_VM_ROOT = "/data/firecracker-vms"
DEFAULT_STATE_FILE = "/var/lib/spire-kit/ledger.json"
DEFAULT_TTL = 600
IMDS_BASE = "http://169.254.169.254"


def log(event: str, level: str = "info", **fields: Any) -> None:
    """结构化单行 JSON 日志。绝不打印 token 明文(只打 sha256 前 8 位 + 长度)。"""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "event": event}
    rec.update(fields)
    print(json.dumps(rec, sort_keys=True, ensure_ascii=False), flush=True)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


# ──────────────────────────────────────────────────────────────────────────────
# VM 注册表:唯一事实源是 launch-vm.sh 自己写的 vm.json,broker 不自己算 vm_num→IP
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VmRecord:
    tenant_id: str
    vm_num: int
    guest_ip: str
    host_tap_ip: str
    vm_dir: str

    @property
    def tap(self) -> str:
        return f"tap-vm{self.vm_num}"


def host_end_of_p2p(guest_ip: str) -> str:
    """/30 的 guest 端(.+2)推 host 端(.+1)。

    只用"同一条 /30 内相邻"这一条构造事实(launch-vm.sh:718-721),不重算
    vm_num→IP 公式 —— 公式漂移过一次的教训见 vm_num/host_port 不可互推。
    """
    addr = ipaddress.IPv4Address(guest_ip)
    return str(addr - 1)


def load_registry(vm_root: str) -> dict[str, VmRecord]:
    """扫 <vm_root>/*/vm.json,返回 {host_tap_ip: VmRecord}。

    坏 json / 缺字段 / 目的 IP 撞车都记日志跳过,绝不让一台坏 VM 拖垮整台 host 的发证。
    """
    registry: dict[str, VmRecord] = {}
    poisoned: set[str] = set()  # 撞过车的 /30 本轮永久拉黑,后来者不能"补位"进来
    root = Path(vm_root)
    if not root.is_dir():
        log("registry_root_missing", level="warn", vm_root=vm_root)
        return registry
    for meta in sorted(root.glob("*/vm.json")):
        try:
            data = json.loads(meta.read_text())
            tenant_id = str(data["tenant_id"])
            vm_num = int(data["vm_num"])
            guest_ip = str(data["guest_ip"])
            host_tap_ip = host_end_of_p2p(guest_ip)
        except Exception as exc:  # noqa: BLE001 - 单条坏记录不能影响其它租户
            log("registry_entry_skipped", level="warn", path=str(meta), error=repr(exc))
            continue
        if host_tap_ip in poisoned:
            log("registry_entry_poisoned", level="warn", host_tap_ip=host_tap_ip, tenant_id=tenant_id)
            continue
        if host_tap_ip in registry and registry[host_tap_ip].tenant_id != tenant_id:
            # 两个 vm.json 声称同一条 /30 → 无法判定归属,两边都不发(fail-closed)。
            log(
                "registry_conflict",
                level="error",
                host_tap_ip=host_tap_ip,
                existing=registry[host_tap_ip].tenant_id,
                incoming=tenant_id,
            )
            registry.pop(host_tap_ip, None)
            poisoned.add(host_tap_ip)
            continue
        registry[host_tap_ip] = VmRecord(
            tenant_id=tenant_id,
            vm_num=vm_num,
            guest_ip=guest_ip,
            host_tap_ip=host_tap_ip,
            vm_dir=str(meta.parent),
        )
    return registry


def reload_record(record: VmRecord, vm_root: str) -> VmRecord | None:
    """签发前【全量重扫】注册表,确认这条 /30 仍然唯一属于同一个租户。

    为什么不能只重读缓存那个目录:VM 目录按 tenant 命名,slot 复用会出现**新目录**占用同一条
    /30(旧目录的 vm.json 可能还残留)。只重读旧目录会一路通过,新租户就拿到旧租户的 token
    —— 独立复审第二轮抓出的绕过路径。全量重扫同时吃到"冲突即拉黑"的保护。
    签发是每 VM 每次开机一次的低频操作,380 个 vm.json 的读开销可以接受。
    """
    fresh_registry = load_registry(vm_root)
    fresh = fresh_registry.get(record.host_tap_ip)
    if fresh is None:
        log("record_gone_or_conflicted", level="warn", host_tap_ip=record.host_tap_ip,
            cached_tenant=record.tenant_id)
        return None
    if fresh != record:
        log("record_changed_since_cache", level="warn",
            cached=f"{record.tenant_id}/{record.vm_num}/{record.guest_ip}/{record.vm_dir}",
            fresh=f"{fresh.tenant_id}/{fresh.vm_num}/{fresh.guest_ip}/{fresh.vm_dir}")
        return None
    return fresh


def taps_all_strict(registry: dict[str, VmRecord], rp_filter_lookup=None,
                    proc_root: str = "/proc/sys/net/ipv4/conf") -> tuple[bool, str]:
    """要求**内核里真实存在的每一条 tap** 的 rp_filter 生效值都是 strict。

    三层理由:
    1. 反向路径过滤生效在**报文入接口**上。攻击者在自己的 tapB 上伪造 tapA 的源地址时,
       起作用的是 tapB —— 只检查"目的记录对应的 tapA"证明不了任何事(第一轮复审抓出;
       我原先那条 netns 断言恰好让入接口==记录 tap,毫无判别力)。
    2. 枚举来源必须是 `/proc`,不能是 registry:坏 json、未登记、或因冲突被拉黑的真实
       `tap-vm*` 不在 registry 里,却照样是可用的伪造入口(第二轮复审抓出)。
    3. **判据必须是生效值 `max(conf/all, conf/<tap>)`,不是 per-tap 那个文件的值**
       —— 见 `rp_filter_value` 的注释。读错对象会在 `all=2` 的真机上给出假绿灯
       (第三轮 Codex 独立复审抓出,2026-08-18 真机实测确认)。
    对外只回不带 tap 名的原因(tap 名对租户是侦察信息),细节留在 host 日志。
    """
    # 默认 lookup 必须跟着 proc_root 走,否则传了 proc_root 的调用方(测试、或将来
    # 的 netns 场景)枚举的是假 /proc、读值却是真 /proc —— 断言会因"读不到→None→非
    # strict"而偶然通过,看着绿其实没测到东西。
    lookup = rp_filter_lookup or (lambda tap: rp_filter_value(tap, proc_root))
    taps = {record.tap for record in registry.values()}
    try:
        taps.update(entry.name for entry in Path(proc_root).iterdir()
                    if entry.name.startswith("tap"))
    except OSError:
        pass  # 非 Linux / 读不到 → 只能按 registry 里的判,下面的 lookup 会返回 None 而拒
    loose = sorted(tap for tap in taps if lookup(tap) != 1)
    if loose:
        log("rp_filter_not_strict", level="error", taps=loose[:8], total_loose=len(loose))
        return False, "rp_filter_not_strict"
    return True, "all_taps_strict"


def rp_filter_value(tap: str, proc_root: str = "/proc/sys/net/ipv4/conf") -> int | None:
    """tap 上**生效**的 rp_filter 值。返回 None 表示读不到(非 Linux / tap 不存在)。

    kernel.org ip-sysctl 原文:"The max value from conf/{all,interface}/rp_filter is
    used when doing source validation on the {interface}"。所以只读
    `conf/<tap>/rp_filter` 是**读错了对象** —— ClawPool host 上 `conf/all/rp_filter=2`
    (平台自己的 `/etc/sysctl.d/10-network-security.conf` 设的 loose),per-tap 设成 1 之后
    生效值仍是 `max(2,1)=2`,而 loose 只要求源地址"经任一接口可达",挡不住同 host 上的
    跨 tap 伪造。旧实现读 per-tap 文件读到 1 就报 strict = **假绿灯**。
    `all` 缺失时按 0 处理(读不到全局值不能当成"更严"),tap 自身缺失仍返回 None。
    """
    try:
        own = int(Path(proc_root, tap, "rp_filter").read_text().strip())
    except Exception:  # noqa: BLE001
        return None
    try:
        shared = int(Path(proc_root, "all", "rp_filter").read_text().strip())
    except Exception:  # noqa: BLE001
        shared = 0
    return max(own, shared)


def rpfilter_match_rules(port: int, runner=None) -> tuple[bool, str]:
    """host 上是否有 `-m rpfilter --invert -j DROP` 在替 broker 端口做 strict 检查。

    为什么需要这条独立于 sysctl 的机制:把 `conf/all/rp_filter` 改成 1 会连带把**主 ENI**
    切成 strict,多 ENI / 策略路由的 host 上可能打断非对称路由 —— 那不是本 kit 能单方面
    替客户决定的。iptables 的 rpfilter 匹配模块在**规则级**做同样的 strict 反向路径检查,
    不看 `conf/all`,且 `-i tap+` 通配能自动覆盖之后新建的 tap。
    2026-08-18 真机 netns 实测:`all=2` 且把三个 tap 的 rp_filter 全设 0 时,该规则仍然
    挡住伪造(命中计数增长、服务端零 accept),正常与跨租户真源流量均不误杀。

    只认 raw/PREROUTING 里命中本端口的规则。读不到 iptables(非 Linux / 无权限)→ False。
    """
    run = runner or _iptables_save_raw
    try:
        dump = run()
    except Exception:  # noqa: BLE001
        return False, "rpfilter_rules_unreadable"
    if dump is None:
        return False, "rpfilter_rules_unreadable"
    for line in dump.splitlines():
        if "rpfilter" not in line or "--invert" not in line:
            continue
        if "-j DROP" not in line:
            continue
        if f"--dport {port}" not in line:
            continue
        return True, "rpfilter_match_rule_present"
    return False, "rpfilter_match_rule_absent"


def _iptables_save_raw() -> str | None:
    """dump raw 表。用 `iptables -t raw -S` 而不是 iptables-save:后者在部分镜像上没装。"""
    proc = subprocess.run(  # noqa: S603
        ["iptables", "-w", "5", "-t", "raw", "-S", "PREROUTING"],
        capture_output=True, text=True, timeout=10, check=False)
    return proc.stdout if proc.returncode == 0 else None


def spoof_guard(port: int, registry: dict[str, VmRecord], rp_filter_lookup=None,
                rpfilter_rule_lookup=None) -> tuple[bool, str]:
    """源地址伪造防护是否存在 —— **两种机制满足其一即可**。

    不能只要求 sysctl strict:ClawPool host 的 `conf/all/rp_filter` 是平台设的 2,
    若把"生效值必须为 1"当唯一判据,`enforce` 会在真机上**拒发全部 token**,平台直接不可用。
    正确的命题是"伪造挡得住吗",而它有两条各自充分的实现路径:
      a) sysctl 生效值 `max(all,iface)==1`(客户自己把 host 收成 strict 时走这条)
      b) iptables raw 表的 rpfilter 匹配规则(本 kit 默认落地的那条,不动 `all`)
    两条都不在才是真的没有防护 → `enforce` 下拒发。
    """
    rule_ok, rule_why = rpfilter_match_rules(
        port, runner=rpfilter_rule_lookup)
    if rule_ok:
        return True, rule_why
    sysctl_ok, sysctl_why = taps_all_strict(registry, rp_filter_lookup)
    if sysctl_ok:
        return True, sysctl_why
    log("spoof_guard_absent", level="error",
        iptables=rule_why, sysctl=sysctl_why, port=port)
    return False, "spoof_guard_absent"


def boot_marker(vm_dir: str) -> str:
    """同一次开机的判别物。

    launch-vm.sh 每次启动都先 `rm -f ${SOCK}`,firecracker 重新 bind 出新的 fc.sock,
    所以 fc.sock 的 mtime_ns 随每次 launch 变化,能区分"同一次开机内重复要 token"
    (拒)和"VM 重启后要新 token"(发)。fc.sock 缺失时回落到 vm.json。
    """
    for name in ("fc.sock", "vm.json"):
        p = Path(vm_dir, name)
        try:
            return f"{name}:{p.stat().st_mtime_ns}"
        except OSError:
            continue
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# 一次性台账
# ──────────────────────────────────────────────────────────────────────────────
class LedgerUnavailable(RuntimeError):
    """台账不可用(坏文件 / 落盘失败)。默认 fail-closed:宁可不发证,也不发无法记账的证。"""


class TokenLedger:
    """记录 (tenant_id, boot_marker) 已发几枚 token,落盘以扛住 broker 重启。

    **claim() 是唯一入口,且在一把锁里完成"检查 + 记账 + 落盘"**。
    早先版本把 check() 与 record() 分开,并发下两个请求会同时通过 check → 配额被穿透
    (独立 Codex review 抓出:上限 1 时 8 并发全部签发)。这类 TOCTOU 只能靠"检查即占用"消除。
    """

    def __init__(self, state_file: str | None, max_per_boot: int = 1,
                 corrupt_policy: str = "fail-closed") -> None:
        self.state_file = state_file
        self.max_per_boot = max_per_boot
        self.corrupt_policy = corrupt_policy
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}
        self.degraded: str | None = None
        if state_file and Path(state_file).is_file():
            try:
                loaded = json.loads(Path(state_file).read_text())
                if not isinstance(loaded, dict):
                    raise ValueError(f"ledger root is {type(loaded).__name__}, expected object")
                self._state = loaded
            except Exception as exc:  # noqa: BLE001
                # 坏台账 = 一次性语义失去依据。默认拒发并把坏文件隔离留证,而不是清空继续发
                # (清空继续发会让"每次开机限量"这条对攻击者变成可复位的)。
                self.degraded = f"ledger_corrupt:{exc!r}"
                log("ledger_corrupt", level="error", path=state_file, error=repr(exc),
                    policy=corrupt_policy)
                try:
                    Path(state_file).replace(Path(f"{state_file}.corrupt"))
                    log("ledger_quarantined", level="warn", path=f"{state_file}.corrupt")
                except OSError as move_exc:
                    log("ledger_quarantine_failed", level="error", error=repr(move_exc))

    def _persist_locked(self) -> None:
        """锁内落盘。失败必须上抛 —— 发出去却没记下的 token 等于没有一次性。"""
        if not self.state_file:
            return
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def claim(self, tenant_id: str, marker: str) -> tuple[bool, str, int]:
        """原子占额:同一把锁里检查配额 → 递增 → 落盘。返回 (是否放行, 原因, 本次序号)。

        序号很关键:同一次开机允许多枚 token 时,幂等键必须逐枚不同 —— 否则 registrar 真做
        去重会把第 2、3 次请求答成"已消费的那枚"(独立复审抓出)。同一枚的重试才复用同键。
        """
        if self.degraded and self.corrupt_policy == "fail-closed":
            return False, self.degraded, 0
        if marker == "unknown":
            # 连开机判别物都读不到 → 无法界定"这一次开机",不发(否则配额可被无限复位)
            return False, "boot_marker_unknown", 0
        with self._lock:
            cur = self._state.get(tenant_id)
            if cur is None or cur.get("marker") != marker:
                cur = {"marker": marker, "count": 0}
                reason = "first_issue_this_boot"
            elif int(cur.get("count", 0)) < self.max_per_boot:
                reason = "under_max_per_boot"
            else:
                return False, "already_issued_this_boot", 0
            seq = int(cur.get("count", 0)) + 1
            cur["count"] = seq
            cur["last_claim_at"] = int(time.time())
            snapshot = {k: dict(v) for k, v in self._state.items()}
            self._state[tenant_id] = cur
            try:
                self._persist_locked()
            except Exception as exc:  # noqa: BLE001
                self._state = snapshot  # 回滚占额,保持内存与磁盘一致
                log("ledger_persist_failed", level="error", path=self.state_file, error=repr(exc))
                return False, "ledger_persist_failed", 0
            return True, reason, seq

    def annotate(self, tenant_id: str, fingerprint: str) -> None:
        """签发成功后补记指纹(审计用;失败不影响已占的额)。"""
        with self._lock:
            cur = self._state.get(tenant_id)
            if not cur:
                return
            cur["last_fingerprint"] = fingerprint
            cur["last_issued_at"] = int(time.time())
            try:
                self._persist_locked()
            except Exception as exc:  # noqa: BLE001
                log("ledger_annotate_failed", level="warn", error=repr(exc))

    def release(self, tenant_id: str, marker: str, seq: int) -> None:
        """把占用的额还回去 —— 必须验明 marker 与序号。

        不校验就还额会出问题:一个卡住的旧请求晚到的 release 会把【新一次开机】的额度
        错误退掉(独立复审抓出)。只有"当前 marker 且当前计数正是我占的那一枚"才回退。
        """
        with self._lock:
            cur = self._state.get(tenant_id)
            if not cur:
                return
            if cur.get("marker") != marker or int(cur.get("count", 0)) != seq:
                log("ledger_release_skipped", level="warn", tenant_id=tenant_id,
                    reason="marker_or_seq_mismatch")
                return
            cur["count"] = max(0, int(cur.get("count", 0)) - 1)
            try:
                self._persist_locked()
            except Exception as exc:  # noqa: BLE001
                log("ledger_release_failed", level="warn", error=repr(exc))


class RateLimiter:
    """每源 IP 的粗粒度令牌桶,挡住 guest 侧疯狂重试打爆 registrar。"""

    def __init__(self, per_minute: int = 12) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < 60.0]
            if len(hits) >= self.per_minute:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


# ──────────────────────────────────────────────────────────────────────────────
# 身份判定(纯函数,便于单测)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    record: VmRecord | None = None


def attest(
    dest_ip: str,
    src_ip: str,
    registry: dict[str, VmRecord],
    rp_filter_policy: str = "enforce",
    rp_filter_lookup=rp_filter_value,
    broker_port: int = 8877,
    rpfilter_rule_lookup=None,
) -> Verdict:
    """把一次连接钉到唯一一台 VM。任何一项不过即 DENY(fail-closed)。"""
    record = registry.get(dest_ip)
    if record is None:
        return Verdict(False, "dest_ip_not_a_tap_gateway")
    if src_ip != record.guest_ip:
        # 跨租户:guest A 打到 tap B 的 host IP(host 本地地址走 INPUT,不受 FORWARD
        # 超网 DROP 覆盖)—— 这条判定就是那个缺口的补丁。
        return Verdict(False, "src_ip_not_paired_guest")
    # 伪造防护:检查【全量】入接口,不是这条记录的 tap —— 伪造发生在攻击者自己的 tap 上。
    # 两种机制满足其一即可(iptables rpfilter 规则 / sysctl 生效值 strict),见 spoof_guard。
    guarded, why = spoof_guard(broker_port, registry, rp_filter_lookup,
                               rpfilter_rule_lookup)
    if not guarded:
        if rp_filter_policy == "enforce":
            return Verdict(False, why)
        log("rp_filter_degraded", level="warn", detail=why, policy=rp_filter_policy)
    return Verdict(True, "attested", record)


# ──────────────────────────────────────────────────────────────────────────────
# Registrar 后端
# ──────────────────────────────────────────────────────────────────────────────
class RegistrarError(RuntimeError):
    """registrar 调用失败,结果【不确定】(超时、连接中断、响应不可解析)。"""


class RegistrarRejected(RegistrarError):
    """registrar 明确拒绝或请求根本没发出去(IID 缺失 / 4xx):可以安全回退配额。"""


def sanitize_header_value(raw: str) -> str:
    """把多行 base64(EC2 IID pkcs7)压成可用的单行 header 值。

    IMDS 返回的 pkcs7 是分行的。带换行的字符串不能当 HTTP header 值(urllib 直接
    `ValueError: Invalid header value`;curl 也会拼出坏 header)—— t4g 真机实测踩到。
    registrar 侧 base64 解码前去空白即可,语义不变。
    """
    value = "".join(raw.split())
    if not value:
        raise RegistrarError("header 值为空")
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in value):
        raise RegistrarError("header 值含非可打印字符,拒绝发送")
    return value


@dataclass
class TokenGrant:
    join_token: str
    ttl: int
    spiffe_id: str
    node_spiffe_id: str
    backend: str


class Registrar:
    """四种后端。前三种是我们提供的,第四种(exec)是**给客户换掉整段 SPIRE 逻辑的插件口**。

    * http  —— POST {url}/v1/entry/register,带 X-Host-Identity(EC2 IID pkcs7)。
               响应兼容两种:客户信封 {"code":200,"data":{join_token,ttl,spiffe_id}}
               或平层 {join_token,ttl,spiffe_id}(registrar-stub / 早期约定)
    * local —— 本机 `spire-server token generate` + `entry create`(单 host / 自建)
    * exec  —— 调客户自己的程序:JSON 走 stdin/stdout。客户想换鉴权方式、换 registrar、
               换 SPIFFE ID 规则、甚至完全不用 SPIRE,都只需放一个可执行文件 + 改配置,
               **不改 ClawPool 任何代码,也不改本 broker 代码**。契约见 §exec-contract。
    * stub  —— 仅当 SPIRE_KIT_ALLOW_STUB=1 时可用的测试桩,启动即大声记日志

    §exec-contract
      stdin  : {"tenant_id","vm_num","guest_ip","host_tap_ip","workload_uid",
                "trust_domain","boot_marker","idempotency_key","seq"}
      stdout : {"join_token","ttl"?,"spiffe_id"?,"node_spiffe_id"?}
      退出码 : 0 = 成功;2 = 明确拒绝(broker 归还配额);其它非 0 = 结果不确定(保留配额)
    """

    def __init__(self, cfg: argparse.Namespace) -> None:
        self.cfg = cfg
        self.backend = cfg.registrar_backend
        self._iid: tuple[float, str] | None = None
        if self.backend == "stub" and os.environ.get("SPIRE_KIT_ALLOW_STUB") != "1":
            raise SystemExit("registrar-backend=stub 需要显式 SPIRE_KIT_ALLOW_STUB=1(仅测试用)")
        if self.backend == "stub":
            log("registrar_stub_enabled", level="warn", note="TEST ONLY — 不签真 SPIRE 身份")

    # -- EC2 Instance Identity Document(host 侧证明自己是哪台 metal)------------
    def host_identity(self) -> str:
        if self._iid and time.time() - self._iid[0] < 300:
            return self._iid[1]
        req = urllib.request.Request(
            f"{IMDS_BASE}/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            imds_token = resp.read().decode()
        req = urllib.request.Request(
            f"{IMDS_BASE}/latest/dynamic/instance-identity/pkcs7",
            headers={"X-aws-ec2-metadata-token": imds_token},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            raw = resp.read().decode()
        # IMDS 返回的 pkcs7 是**分行**的 base64。带换行的字符串不能当 HTTP header 值
        # (urllib 直接 ValueError: Invalid header value;curl 也会把它拼成坏 header)——
        # 真机 t4g 上实测踩到,必须压成单行。registrar 侧解码前把空白去掉即可。
        pkcs7 = sanitize_header_value(raw)
        self._iid = (time.time(), pkcs7)
        return pkcs7

    # -- 可达性探测(给 /healthz 用)-------------------------------------------
    def probe(self) -> dict[str, Any]:
        """探"发证依赖此刻是否可达",不发证。

        为什么要有:原来的 /healthz 只证明"进程活着 + 台账没坏"。broker 全绿而 registrar
        跨 VPC 不通 / spire-server socket 不存在,是最常见的交付故障,却完全不体现在
        健康状态里 —— 于是"绿灯"给了错误的信心,真正的失败要等第一台 VM 起来才暴露。
        探测结果缓存 30s:healthz 可能被监控高频拉,不能每次都去打客户的 registrar。
        """
        now = time.time()
        cached = getattr(self, "_probe_cache", None)
        if cached and now - cached[0] < 30:
            return cached[1]
        result: dict[str, Any] = {"backend": self.backend}
        try:
            if self.backend == "http":
                # 只建 TCP 连接,不发 HTTP 请求:registrar 未必有健康端点,而"端口通不通"
                # 已经覆盖了跨 VPC 路由 / SG / DNS 这几个真正的故障源。
                url = urllib.parse.urlsplit(self.cfg.registrar_url)
                port = url.port or (443 if url.scheme == "https" else 80)
                if not url.hostname:
                    raise ValueError(f"registrar_url 解析不出主机名: {self.cfg.registrar_url!r}")
                with socket.create_connection((url.hostname, port), timeout=2):
                    pass
                result.update(reachable=True, target=f"{url.hostname}:{port}")
            elif self.backend == "local":
                sock_ok = Path(self.cfg.spire_server_socket).exists()
                result.update(reachable=sock_ok, target=self.cfg.spire_server_socket)
                if not sock_ok:
                    result["error"] = "spire-server socket 不存在"
                    # 本 unit 有 PrivateTmp=yes:宿主 /tmp 下的 socket 对本进程不可见。
                    # 不点出这一条,现象就是"ss -xln 明明看得到 socket,broker 却说不存在",
                    # 排查会直奔 spire-server 而不是 systemd 沙箱(真机上就走过这条弯路)。
                    if self.cfg.spire_server_socket.startswith("/tmp/"):
                        result["error"] += (
                            "(路径在 /tmp 下,而本 unit 有 PrivateTmp=yes —— 宿主 /tmp 里的 "
                            "socket 对本进程不可见,即使 ss -xln 看得到。请把 spire-server 的 "
                            "socket_path 配到 /run/spire-server/private/api.sock)"
                        )
            elif self.backend == "exec":
                cmd_ok = bool(self.cfg.registrar_cmd) and os.access(self.cfg.registrar_cmd, os.X_OK)
                result.update(reachable=cmd_ok, target=self.cfg.registrar_cmd or "(未配)")
                if not cmd_ok:
                    result["error"] = "registrar-cmd 不存在或不可执行"
            else:
                result.update(reachable=True, target="stub")
        except Exception as exc:  # noqa: BLE001 - 探测失败本身就是要报告的结果
            result.update(reachable=False, error=repr(exc)[:200])
        self._probe_cache = (now, result)
        return result

    def issue(self, tenant_id: str, idempotency_key: str = "", context: dict | None = None) -> TokenGrant:
        self._context = context or {}
        if self.backend == "http":
            return self._issue_http(tenant_id, idempotency_key)
        if self.backend == "local":
            return self._issue_local(tenant_id)
        if self.backend == "exec":
            return self._issue_exec(tenant_id, idempotency_key)
        return self._issue_stub(tenant_id)

    def _issue_http(self, tenant_id: str, idempotency_key: str = "") -> TokenGrant:
        body = json.dumps({"tenant_id": tenant_id, "workload_uid": self.cfg.workload_uid}).encode()
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            # 幂等键:服务端成功但响应丢失时,重试不该变成"多发一枚"。registrar 侧支持前
            # 这只是声明意图,所以下面同时把"非幂等 POST 的自动重试"限制到可重试错误。
            headers["Idempotency-Key"] = idempotency_key
        try:
            headers["X-Host-Identity"] = self.host_identity()
        except Exception as exc:  # noqa: BLE001
            log("host_identity_unavailable", level="error", error=repr(exc))
            if self.cfg.require_host_identity:
                # fail-closed:拿不到 host 身份就不要向 registrar 讨证(独立 review 意见)
                raise RegistrarRejected(f"host identity unavailable: {exc!r}") from exc
        last: Exception | None = None
        for attempt in range(1, self.cfg.registrar_retries + 1):
            req = urllib.request.Request(
                f"{self.cfg.registrar_url.rstrip('/')}/v1/entry/register",
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.registrar_timeout) as resp:
                    payload = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as exc:
                # 4xx 是"请求本身不对"(除 429),重试只会放大压力并可能多发 token
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise RegistrarRejected(f"registrar rejected request: HTTP {exc.code}") from exc
                last = exc
                log("registrar_attempt_failed", level="warn", attempt=attempt,
                    status=exc.code, retryable=True)
                time.sleep(min(2 ** (attempt - 1), 4))
            except Exception as exc:  # noqa: BLE001 - 网络类错误可重试
                last = exc
                log("registrar_attempt_failed", level="warn", attempt=attempt, error=repr(exc))
                time.sleep(min(2 ** (attempt - 1), 4))
        else:
            raise RegistrarError(f"registrar unreachable after {self.cfg.registrar_retries} tries: {last!r}")
        # 客户 registrar(2026-08-20 定稿)返回信封格式:{"code": 200, "data": {...}};
        # registrar-stub 与早期约定是平层 {...}。两种都接受。信封判定看 code 而不是
        # data:拒绝时(如 {"code":403,"message":...})**没有 data 字段**,只认 data
        # 会把明确拒绝误报成"结果不确定"(RegistrarError→不归还配额)。code != 200
        # 视为业务层拒绝(HTTP 层已 200,不重试——重试只会再拒一次)。
        envelope_code = payload.get("code")
        if envelope_code is not None:
            try:
                envelope_code = int(envelope_code)
            except (TypeError, ValueError):
                raise RegistrarError(f"registrar envelope code 不是数字: {envelope_code!r}")
            if envelope_code != 200:
                raise RegistrarRejected(
                    f"registrar envelope code={envelope_code}(HTTP 200 但业务层拒绝)")
            if not isinstance(payload.get("data"), dict):
                raise RegistrarError("registrar envelope code=200 但缺 data 对象")
            payload = payload["data"]
        token = str(payload.get("join_token") or "")
        if not token:
            raise RegistrarError(f"registrar returned no join_token: keys={sorted(payload)}")
        return TokenGrant(
            join_token=token,
            ttl=int(payload.get("ttl") or self.cfg.token_ttl),
            spiffe_id=str(payload.get("spiffe_id") or self._workload_id(tenant_id)),
            node_spiffe_id=str(payload.get("node_spiffe_id") or self._node_id(tenant_id)),
            backend="http",
        )

    def _issue_local(self, tenant_id: str) -> TokenGrant:
        node_id = self._node_id(tenant_id)
        workload_id = self._workload_id(tenant_id)
        gen = subprocess.run(
            [
                self.cfg.spire_server_bin, "token", "generate",
                "-spiffeID", node_id,
                "-ttl", str(self.cfg.token_ttl),
                "-socketPath", self.cfg.spire_server_socket,
            ],
            capture_output=True, text=True, timeout=self.cfg.registrar_timeout,
        )
        if gen.returncode != 0:
            raise RegistrarError(f"token generate rc={gen.returncode} stderr={gen.stderr.strip()[:200]}")
        token = ""
        for line in gen.stdout.splitlines():
            if line.lower().startswith("token:"):
                token = line.split(":", 1)[1].strip()
        if not token:
            # 绝不回显原始 stdout —— 输出格式一变就可能把 token 明文带进日志(独立 review 意见)
            raise RegistrarError(
                f"token generate produced no token (stdout {len(gen.stdout)} bytes, "
                f"{len(gen.stdout.splitlines())} lines)")
        # workload entry 幂等:已存在按成功处理(SPIRE 对重复 entry 返非零 + already exists)
        ent = subprocess.run(
            [
                self.cfg.spire_server_bin, "entry", "create",
                "-parentID", node_id,
                "-spiffeID", workload_id,
                "-selector", f"unix:uid:{self.cfg.workload_uid}",
                "-socketPath", self.cfg.spire_server_socket,
            ],
            capture_output=True, text=True, timeout=self.cfg.registrar_timeout,
        )
        if ent.returncode != 0 and "already exists" not in (ent.stdout + ent.stderr).lower():
            raise RegistrarError(f"entry create rc={ent.returncode} stderr={ent.stderr.strip()[:200]}")
        return TokenGrant(token, self.cfg.token_ttl, workload_id, node_id, "local")

    def _issue_exec(self, tenant_id: str, idempotency_key: str) -> TokenGrant:
        """把发证这件事整段交给客户的程序(插件口)。"""
        cmd = self.cfg.registrar_cmd
        if not cmd:
            raise RegistrarRejected("registrar-backend=exec 但没给 --registrar-cmd")
        # context(vm_num/guest_ip/host_tap_ip/boot_marker/seq)先铺底,四个身份键后写:
        # 反过来 update(_context) 会让 context 覆盖已构造好的 tenant_id/workload_uid/
        # trust_domain/idempotency_key —— 上游哪天往 context 里塞了同名键,插件收到的
        # 就是被偷换过的身份(#516 交接遗留债②)。
        payload = dict(getattr(self, "_context", {}) or {})
        payload.update({
            "tenant_id": tenant_id,
            "workload_uid": self.cfg.workload_uid,
            "trust_domain": self.cfg.trust_domain,
            "idempotency_key": idempotency_key,
        })
        try:
            out = subprocess.run([cmd], input=json.dumps(payload), capture_output=True,
                                 text=True, timeout=self.cfg.registrar_timeout)
        except subprocess.TimeoutExpired as exc:
            raise RegistrarError(f"registrar-cmd 超时(结果不确定): {cmd}") from exc
        if out.returncode == 2:
            # 约定:退出码 2 = 明确拒绝,没发出去 → broker 可以安全归还配额
            raise RegistrarRejected(f"registrar-cmd 明确拒绝(rc=2): {out.stderr.strip()[:200]}")
        if out.returncode != 0:
            raise RegistrarError(f"registrar-cmd rc={out.returncode}(结果不确定)")
        try:
            data = json.loads(out.stdout)
        except Exception as exc:  # noqa: BLE001 - 不回显 stdout,可能含 token
            raise RegistrarError(
                f"registrar-cmd 输出不是 JSON({len(out.stdout)} bytes)") from exc
        token = str(data.get("join_token") or "")
        if not token:
            raise RegistrarError(f"registrar-cmd 没给 join_token: keys={sorted(data)}")
        return TokenGrant(
            join_token=token,
            ttl=int(data.get("ttl") or self.cfg.token_ttl),
            spiffe_id=str(data.get("spiffe_id") or self._workload_id(tenant_id)),
            node_spiffe_id=str(data.get("node_spiffe_id") or self._node_id(tenant_id)),
            backend="exec",
        )

    def _issue_stub(self, tenant_id: str) -> TokenGrant:
        digest = hashlib.sha256(f"{tenant_id}:{time.time_ns()}".encode()).hexdigest()
        token = f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        return TokenGrant(token, self.cfg.token_ttl, self._workload_id(tenant_id), self._node_id(tenant_id), "stub")

    def _node_id(self, tenant_id: str) -> str:
        return f"spiffe://{self.cfg.trust_domain}/node/{tenant_id}"

    def _workload_id(self, tenant_id: str) -> str:
        return f"spiffe://{self.cfg.trust_domain}/openclaw/{tenant_id}"


# ──────────────────────────────────────────────────────────────────────────────
# HTTP 面
# ──────────────────────────────────────────────────────────────────────────────
# 对外只暴露粗粒度原因的判定:这两条能被用来扫"哪些 /30 存在",细节只进日志
OPAQUE_REASONS = {"dest_ip_not_a_tap_gateway", "src_ip_not_paired_guest"}


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "spire-join-broker/1.0"
    # 刻意用 HTTP/1.0 语义:不做 keep-alive,一个连接只服务一个请求,压掉慢连接占线程的窗口
    protocol_version = "HTTP/1.0"
    # 慢连接防护:BaseHTTPRequestHandler 用它给 rfile 设超时,避免 guest 挂着不发完请求
    # 就占住一个线程(thread-per-request 下可跨租户耗尽,独立 review 意见)
    timeout = 10

    # 关掉 BaseHTTPRequestHandler 的 stderr 访问日志,统一走结构化 log()
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        return

    @property
    def broker(self) -> "Broker":
        return self.server.broker  # type: ignore[attr-defined]

    def _reply(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = self.path.split("?", 1)[0]
        dest_ip = self.connection.getsockname()[0]
        src_ip = self.client_address[0]
        if path == "/healthz":
            # 只给本机:VM 总数与后端类型对租户没有用处,却是现成的侦察面
            if dest_ip != "127.0.0.1" or src_ip != "127.0.0.1":
                log("healthz_denied", level="warn", dest=dest_ip, src=src_ip)
                self._reply(404, {"error": "not_found"})
                return
            ledger_state = "ok" if not self.broker.ledger.degraded else self.broker.ledger.degraded
            registrar = self.broker.registrar.probe()
            # 伪造防护也要进 healthz:这一格缺失过一轮,导致"防护其实没生效"只能靠人去
            # 读 sysctl 才发现。enforce 下它决定发不发证,属于必须可观测的状态。
            guarded, guard_why = spoof_guard(self.broker.cfg.port, self.broker.registry())
            # ok 必须同时包含"发证依赖可达"与"伪造防护在位"—— 否则绿灯只代表进程活着,
            # 而那正是交付期最容易误导人的假绿灯(broker 全绿、guest 却永远 attest 不上;
            # 或 broker 全绿、防护其实是关的)。
            enforcing = self.broker.cfg.rp_filter_policy == "enforce"
            self._reply(200, {"ok": (ledger_state == "ok"
                                     and bool(registrar.get("reachable"))
                                     and (guarded or not enforcing)),
                              "vms": len(self.broker.registry()),
                              "backend": self.broker.cfg.registrar_backend,
                              "ledger": ledger_state,
                              "spoof_guard": {"present": guarded, "via": guard_why,
                                              "policy": self.broker.cfg.rp_filter_policy},
                              "registrar": registrar})
            return
        if path == "/v1/join-token":
            self.broker.handle_join_token(self, dest_ip, src_ip)
            return
        self._reply(404, {"error": "not_found"})


class Broker:
    def __init__(self, cfg: argparse.Namespace) -> None:
        self.cfg = cfg
        self.registrar = Registrar(cfg)
        self.ledger = TokenLedger(cfg.state_file, cfg.max_issues_per_boot, cfg.ledger_corrupt_policy)
        self.limiter = RateLimiter(cfg.rate_limit_per_minute)
        self._slots = threading.Semaphore(cfg.max_concurrency)
        self._registry: dict[str, VmRecord] = {}
        self._registry_ts = 0.0
        self._registry_lock = threading.Lock()

    def registry(self) -> dict[str, VmRecord]:
        """带 TTL 的注册表缓存:新 VM 最迟 registry_ttl 秒后可见。"""
        with self._registry_lock:
            if time.time() - self._registry_ts > self.cfg.registry_ttl:
                self._registry = load_registry(self.cfg.vm_root)
                self._registry_ts = time.time()
            return self._registry

    def handle_join_token(self, handler: BrokerHandler, dest_ip: str, src_ip: str) -> None:
        if not self.cfg.enabled:
            # 可开关:关掉时不发证也不判定,guest 侧退避重试,OpenClaw 完全不受影响。
            # 想彻底移除请跑 install.sh --uninstall(连规则和 unit 一起撤)。
            log("disabled", level="warn", dest=dest_ip, src=src_ip)
            handler._reply(503, {"error": "disabled", "hint": "SPIRE_KIT_ENABLED=false"})
            return
        if not self.limiter.allow(src_ip):
            log("denied", level="warn", dest=dest_ip, src=src_ip, reason="rate_limited")
            handler._reply(429, {"error": "rate_limited"})
            return
        try:
            verdict = attest(dest_ip, src_ip, self.registry(), self.cfg.rp_filter_policy,
                             broker_port=self.cfg.port)
            if not verdict.allowed or verdict.record is None:
                log("denied", level="warn", dest=dest_ip, src=src_ip, reason=verdict.reason)
                shown = "not_attested" if verdict.reason in OPAQUE_REASONS else verdict.reason
                handler._reply(403, {"error": "not_attested", "reason": shown})
                return
            # 缓存不能当授权依据:签发前把这条 vm.json 重新读一遍,确认归属没变
            record = reload_record(verdict.record, self.cfg.vm_root)
            if record is None:
                log("denied", level="warn", dest=dest_ip, src=src_ip, reason="registry_stale")
                handler._reply(409, {"error": "registry_stale"})
                return
            marker = boot_marker(record.vm_dir)
            ok, why, seq = self.ledger.claim(record.tenant_id, marker)
            if not ok:
                log("denied", level="warn", tenant_id=record.tenant_id, reason=why, boot_marker=marker)
                status = 503 if why.startswith(("ledger_", "boot_marker")) else 409
                handler._reply(status, {"error": "not_issued", "reason": why})
                return
            try:
                grant = self.registrar.issue(
                    record.tenant_id,
                    # 幂等键含序号:同一枚的重试复用同键,不同枚必须不同键
                    idempotency_key=hashlib.sha256(
                        f"{record.tenant_id}:{marker}:{seq}".encode()).hexdigest(),
                    context={"vm_num": record.vm_num, "guest_ip": record.guest_ip,
                             "host_tap_ip": record.host_tap_ip,
                             "boot_marker": marker, "seq": seq},
                )
            except RegistrarRejected as exc:
                # 明确没发出去(请求就被拒:IID 缺失 / 4xx)→ 安全地把额还回去
                self.ledger.release(record.tenant_id, marker, seq)
                log("registrar_rejected", level="error", tenant_id=record.tenant_id, error=repr(exc))
                handler._reply(503, {"error": "registrar_unavailable"})
                return
            except Exception as exc:  # noqa: BLE001 - 结果不确定(超时/连接中断)
                # 不确定就【不还额】:服务端可能已经签了。宁可这次开机少一枚配额,
                # 也不要把"可能已签发"的额度重新放出去(独立复审意见)。
                log("registrar_failed", level="error", tenant_id=record.tenant_id,
                    error=repr(exc), quota_kept=True)
                handler._reply(503, {"error": "registrar_unavailable"})
                return
            self.ledger.annotate(record.tenant_id, token_fingerprint(grant.join_token))
            # 成功路径的日志与 200 必须在 try 内:早先误落进 finally,导致每条 403/409/503
            # 回完之后再去读未赋值的 record/grant → UnboundLocalError(独立复审抓出;
            # 我的断言只看状态码,所以这类"回完再炸"完全测不到,已补 no-traceback 回归)。
            log(
                "issued",
                tenant_id=record.tenant_id,
                vm_num=record.vm_num,
                dest=dest_ip,
                src=src_ip,
                backend=grant.backend,
                spiffe_id=grant.spiffe_id,
                token_fingerprint=token_fingerprint(grant.join_token),
                token_len=len(grant.join_token),
                boot_marker=marker,
            )
            handler._reply(
                200,
                {
                    "join_token": grant.join_token,
                    "ttl": grant.ttl,
                    "tenant_id": record.tenant_id,
                    "spiffe_id": grant.spiffe_id,
                    "node_spiffe_id": grant.node_spiffe_id,
                    "trust_domain": self.cfg.trust_domain,
                    "server_address": self.cfg.spire_server_address,
                    "server_port": str(self.cfg.spire_server_port),
                    "issued_at": int(time.time()),
                },
            )
        finally:
            pass  # 并发闸在 server 层(建线程前)完成,这里不再重复计数


class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    slots: threading.Semaphore | None = None

    def process_request(self, request, client_address):
        """在**创建线程之前**卡并发上限。

        早先只在 do_GET 里拿信号量,那时请求行/header 已解析、线程已建 —— 慢速发送方仍能
        无限占线程(独立复审第二轮抓出)。这里改成:占不到槽位就直接关连接,不进线程池。
        """
        if self.slots is not None and not self.slots.acquire(blocking=False):
            log("connection_rejected", level="warn", src=client_address[0], reason="server_busy")
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            if self.slots is not None:
                self.slots.release()
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            if self.slots is not None:
                self.slots.release()


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(description="host 侧 per-microVM SPIRE join-token 代发器")
    p.add_argument("--bind", default=env("SPIRE_KIT_BIND", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(env("SPIRE_KIT_PORT", str(DEFAULT_PORT))))
    p.add_argument("--vm-root", default=env("SPIRE_KIT_VM_ROOT", DEFAULT_VM_ROOT))
    p.add_argument("--registry-ttl", type=float, default=float(env("SPIRE_KIT_REGISTRY_TTL", "5")))
    p.add_argument("--state-file", default=env("SPIRE_KIT_STATE_FILE", DEFAULT_STATE_FILE))
    # 默认 3 而不是 1:agent 崩溃后 systemd 重启要重新 attest,而 join token 是一次性的
    # (SPIRE server 侧强制单次使用)。上游 ByAI 文档把这条列成"需告警"的未解项;这里用
    # "每次开机最多换 3 枚新 token"给崩溃恢复留窗口。多发的是**新** token 而非重放,
    # 且都只映射到该租户自己的 SPIFFE ID,不构成越权面;台账逐次留痕可审计。
    p.add_argument("--max-issues-per-boot", type=int, default=int(env("SPIRE_KIT_MAX_ISSUES", "3")))
    p.add_argument("--rate-limit-per-minute", type=int, default=int(env("SPIRE_KIT_RATE_LIMIT", "12")))
    p.add_argument("--max-concurrency", type=int, default=int(env("SPIRE_KIT_MAX_CONCURRENCY", "32")),
                   help="同时处理的领证请求上限;超了返 503,防慢连接跨租户耗线程")
    p.add_argument("--ledger-corrupt-policy", choices=("fail-closed", "fail-open"),
                   default=env("SPIRE_KIT_LEDGER_CORRUPT_POLICY", "fail-closed"),
                   help="台账文件坏掉时:fail-closed 停发(默认,保住一次性语义)/ fail-open 清零继续发")
    p.add_argument("--require-host-identity", dest="require_host_identity", action="store_true",
                   default=env("SPIRE_KIT_REQUIRE_HOST_IDENTITY", "1") != "0",
                   help="http 后端下取不到 EC2 IID 就拒发(默认开)")
    p.add_argument("--allow-missing-host-identity", dest="require_host_identity", action="store_false",
                   help="允许没有 IID 也向 registrar 讨证(仅无 IMDS 的联调环境)")
    p.add_argument(
        "--rp-filter-policy",
        choices=("enforce", "warn"),
        default=env("SPIRE_KIT_RP_FILTER_POLICY", "enforce"),
        help="enforce:tap 的 rp_filter 非 strict(1) 即拒发(默认);warn:只告警(测试/无 tap 环境)",
    )
    # trust-domain / spire-server-address / registrar-url 刻意【没有】默认值:见 validate_config。
    p.add_argument("--trust-domain", default=env("SPIRE_KIT_TRUST_DOMAIN", ""))
    p.add_argument("--spire-server-address", default=env("SPIRE_KIT_SERVER_ADDRESS", ""))
    p.add_argument("--spire-server-port", default=env("SPIRE_KIT_SERVER_PORT", "8081"))
    p.add_argument("--workload-uid", type=int, default=int(env("SPIRE_KIT_WORKLOAD_UID", "1000")))
    p.add_argument("--token-ttl", type=int, default=int(env("SPIRE_KIT_TOKEN_TTL", str(DEFAULT_TTL))))
    p.add_argument(
        "--registrar-backend",
        choices=("http", "local", "exec", "stub"),
        default=env("SPIRE_KIT_REGISTRAR_BACKEND", "http"),
        help="http=客户 registrar · local=本机 spire-server · exec=客户自己的程序(插件口) · stub=测试",
    )
    p.add_argument("--registrar-cmd", default=env("SPIRE_KIT_REGISTRAR_CMD", ""),
                   help="exec 后端要调的可执行文件(JSON 走 stdin/stdout;契约见 Registrar 文档串)")
    p.add_argument("--enabled", dest="enabled", action="store_true",
                   default=env("SPIRE_KIT_ENABLED", "true").lower() not in ("0", "false", "no"),
                   help="总开关(默认开);关掉后领证请求一律 503,不影响 OpenClaw")
    p.add_argument("--disabled", dest="enabled", action="store_false",
                   help="关掉发证(保留进程与规则,便于快速回切)")
    p.add_argument("--registrar-url", default=env("SPIRE_KIT_REGISTRAR_URL", ""))
    p.add_argument("--registrar-timeout", type=float, default=float(env("SPIRE_KIT_REGISTRAR_TIMEOUT", "5")))
    p.add_argument("--registrar-retries", type=int, default=int(env("SPIRE_KIT_REGISTRAR_RETRIES", "3")))
    p.add_argument("--spire-server-bin", default=env("SPIRE_KIT_SERVER_BIN", "/usr/local/bin/spire-server"))
    # 默认值刻意【不是】spire-server 自己的默认 /tmp/spire-server/private/api.sock:
    # 本 kit 自带的 unit 有 PrivateTmp=yes,broker 看到的是一个私有的空 /tmp,宿主
    # /tmp 下的 socket 对它永远不存在。那会变成"装完 healthz 报红、但 ss -xln 明明
    # 看得到 socket"这种极难查的现象(真机上确实先踩了一次)。
    # /run 不受 PrivateTmp 影响,也是 socket 该放的位置;spire-server 侧配
    # `socket_path = "/run/spire-server/private/api.sock"` + unit 加
    # `RuntimeDirectory=spire-server` 即可。见 hooks/README.md 的 local 后端一节。
    p.add_argument("--spire-server-socket", default=env("SPIRE_KIT_SERVER_SOCKET", "/run/spire-server/private/api.sock"))
    p.add_argument("--print-config", action="store_true", help="打印生效配置后退出(部署自检用)")
    return p


def validate_config(cfg: argparse.Namespace) -> None:
    """启动前拒绝"看起来能跑但注定 attest 失败"的配置。

    这三个值早期有内置默认值(`a1.1o` / `127.0.0.1` /
    `entry-registrar.spire.svc:8080`)。后果不是启动失败,而是**静默错误**:broker 起得来、
    healthz 返回 ok、token 也真发出去了,而 guest 拿着 server_address=127.0.0.1 去连自己的
    8081,node attestation 永远失败。排查时 host 侧全是绿的,方向会被彻底带偏。

    宁可拒绝启动:systemd 起不来是刺眼的、5 秒能定位的失败。

    `stub` 后端豁免:它签不出真身份,且已有 `SPIRE_KIT_ALLOW_STUB=1` 这道更硬的闸
    (在 Registrar.__init__ 里)。在这里也拦会把那道闸的报错盖掉,让"为什么起不来"变模糊。
    """
    if cfg.registrar_backend == "stub":
        return
    problems: list[str] = []
    if not cfg.trust_domain:
        problems.append("缺 --trust-domain / SPIRE_KIT_TRUST_DOMAIN")
    if not cfg.spire_server_address:
        problems.append("缺 --spire-server-address / SPIRE_KIT_SERVER_ADDRESS")
    elif cfg.spire_server_address in ("127.0.0.1", "localhost", "::1"):
        # guest 是在自己的 netns 里连这个地址,loopback 指向 guest 自己
        problems.append(
            f"--spire-server-address={cfg.spire_server_address} 指向本机;"
            "guest 会去连自己,attestation 永远失败"
        )
    if cfg.registrar_backend == "http" and not cfg.registrar_url:
        problems.append("registrar-backend=http 但缺 --registrar-url / SPIRE_KIT_REGISTRAR_URL")
    if cfg.registrar_backend == "exec" and not cfg.registrar_cmd:
        problems.append("registrar-backend=exec 但缺 --registrar-cmd / SPIRE_KIT_REGISTRAR_CMD")
    if problems:
        for p in problems:
            log("config_invalid", level="error", problem=p)
        raise SystemExit(
            "配置不完整,拒绝启动(避免『一路绿灯但 attestation 永远失败』):"
            + "; ".join(problems)
        )


def main(argv: list[str] | None = None) -> int:
    cfg = build_parser().parse_args(argv)
    if cfg.print_config:
        print(json.dumps(vars(cfg), sort_keys=True, indent=2))
        return 0
    validate_config(cfg)
    broker = Broker(cfg)
    server = ReusableServer((cfg.bind, cfg.port), BrokerHandler)
    server.broker = broker  # type: ignore[attr-defined]
    server.slots = broker._slots  # 建线程前就卡并发
    log(
        "listening",
        bind=cfg.bind,
        port=cfg.port,
        vm_root=cfg.vm_root,
        backend=cfg.registrar_backend,
        trust_domain=cfg.trust_domain,
        rp_filter_policy=cfg.rp_filter_policy,
        vms=len(broker.registry()),
        pid=os.getpid(),
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("shutdown")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    socket.setdefaulttimeout(30)
    sys.exit(main())
