#!/usr/bin/env python3
"""logic_probe.py —— broker 判定逻辑的可执行断言(不依赖 pytest,能在 host 上直接跑)

输出协议:每条断言一行 `ASSERT ok <name>` / `ASSERT fail <name> :: <detail>`,
结尾 `TOTAL <n> FAILED <m>`;有任何 fail 则退出码 1。acceptance 脚本按这个协议计数。

同一批判定在 tests/test_spire_kit_broker.py 里也有 pytest 版(CI 用),两处共用同一
模块函数,不复制实现。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent


def load_broker():
    spec = importlib.util.spec_from_file_location("spire_join_broker", KIT_DIR / "spire-join-broker.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 先登记进 sys.modules 再 exec:py3.14 的 dataclasses 会回查 sys.modules[cls.__module__],
    # 不登记会在 @dataclass 处炸 AttributeError(实测 Python 3.14.5)。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Probe:
    def __init__(self) -> None:
        self.total = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        self.total += 1
        if cond:
            print(f"ASSERT ok {name}")
        else:
            self.failed += 1
            print(f"ASSERT fail {name} :: {detail}")

    def finish(self) -> int:
        print(f"TOTAL {self.total} FAILED {self.failed}")
        return 1 if self.failed else 0


def write_vm(root: Path, tenant: str, vm_num: int, guest_ip: str, with_sock: bool = True) -> Path:
    d = root / tenant
    d.mkdir(parents=True, exist_ok=True)
    (d / "vm.json").write_text(
        json.dumps({"tenant_id": tenant, "vm_num": vm_num, "guest_ip": guest_ip, "vcpu": 2, "mem_mb": 2048})
    )
    if with_sock:
        (d / "fc.sock").write_bytes(b"")
    return d


def main() -> int:
    b = load_broker()
    p = Probe()

    # ── /30 相邻推导 ──────────────────────────────────────────────────────────
    p.check("p2p_host_end_from_guest", b.host_end_of_p2p("10.0.0.2") == "10.0.0.1",
            b.host_end_of_p2p("10.0.0.2"))
    p.check("p2p_host_end_crosses_octet", b.host_end_of_p2p("10.0.1.2") == "10.0.1.1",
            b.host_end_of_p2p("10.0.1.2"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_vm(root, "t-aaa", 1, "10.0.0.2")
        write_vm(root, "t-bbb", 2, "10.0.0.6")
        (root / "t-broken").mkdir()
        (root / "t-broken" / "vm.json").write_text("{not json")
        reg = b.load_registry(str(root))

        # ── 注册表 ────────────────────────────────────────────────────────────
        p.check("registry_maps_two_vms", set(reg) == {"10.0.0.1", "10.0.0.5"}, str(sorted(reg)))
        p.check("registry_tenant_binding", reg["10.0.0.1"].tenant_id == "t-aaa", reg["10.0.0.1"].tenant_id)
        p.check("registry_skips_bad_json", all(r.tenant_id != "t-broken" for r in reg.values()), "bad json leaked")
        p.check("registry_tap_name", reg["10.0.0.5"].tap == "tap-vm2", reg["10.0.0.5"].tap)

        strict = lambda tap: 1  # noqa: E731 - 测试替身:所有 tap 都 strict
        loose = lambda tap: 0  # noqa: E731
        # 伪造防护有两条机制(sysctl 生效值 / iptables rpfilter 规则),满足其一即放行。
        # 这些断言要测的是 sysctl 那条,所以显式注入"iptables 里没有规则" —— 否则结果
        # 取决于跑探针的那台机器上有没有那条规则,断言就没有判别力了。
        no_rule = lambda: ""  # noqa: E731
        has_rule = lambda: (  # noqa: E731
            "-A PREROUTING -i tap+ -p tcp -m tcp --dport 8877 -m rpfilter --invert -j DROP\n")

        # ── 身份判定矩阵 ──────────────────────────────────────────────────────
        v = b.attest("10.0.0.1", "10.0.0.2", reg, "enforce", strict)
        p.check("attest_allows_own_pair", v.allowed and v.record.tenant_id == "t-aaa", v.reason)

        v = b.attest("10.0.0.1", "10.0.0.6", reg, "enforce", strict)
        p.check("attest_denies_cross_tenant_src", (not v.allowed) and v.reason == "src_ip_not_paired_guest", v.reason)

        v = b.attest("10.0.0.9", "10.0.0.10", reg, "enforce", strict)
        p.check("attest_denies_unknown_dest", (not v.allowed) and v.reason == "dest_ip_not_a_tap_gateway", v.reason)

        v = b.attest("10.0.0.1", "192.168.1.5", reg, "enforce", strict)
        p.check("attest_denies_foreign_src", not v.allowed, v.reason)

        v = b.attest("10.0.0.1", "10.0.0.2", reg, "enforce", loose,
                     rpfilter_rule_lookup=no_rule)
        p.check("attest_denies_when_spoof_guard_absent",
                (not v.allowed) and v.reason == "spoof_guard_absent", v.reason)

        # 真机的实际形态:sysctl 层因 conf/all=2 失效,但 iptables rpfilter 规则在挡 → 必须放行。
        # 若把"sysctl 必须 strict"当唯一判据,enforce 会在 ClawPool host 上拒发全部 token。
        v = b.attest("10.0.0.1", "10.0.0.2", reg, "enforce", loose,
                     rpfilter_rule_lookup=has_rule)
        p.check("attest_allows_when_iptables_rpfilter_rule_present",
                v.allowed and v.record.tenant_id == "t-aaa", v.reason)

        v = b.attest("10.0.0.1", "10.0.0.2", reg, "warn", loose,
                     rpfilter_rule_lookup=no_rule)
        p.check("attest_warn_policy_allows_loose", v.allowed, v.reason)

        # ── /30 反向:host 端不能当 guest 端用 ────────────────────────────────
        v = b.attest("10.0.0.1", "10.0.0.1", reg, "enforce", strict)
        p.check("attest_denies_src_equals_gateway", not v.allowed, v.reason)

        # ── 冲突的 vm.json:两台 VM 声称同一条 /30 → 双方都不发 ──────────────
        write_vm(root, "t-ccc", 3, "10.0.0.2")  # 与 t-aaa 撞同一条 /30
        reg2 = b.load_registry(str(root))
        p.check("registry_conflict_fails_closed", "10.0.0.1" not in reg2, str(sorted(reg2)))

        # 撞过车的 /30 不能被后来者"补位"占回去(否则攻击者只要多写一份 vm.json 就能改归属)
        write_vm(root, "t-ddd", 4, "10.0.0.2")
        reg3 = b.load_registry(str(root))
        p.check("registry_conflict_stays_poisoned", "10.0.0.1" not in reg3, str(sorted(reg3)))

        # ── boot marker + 一次性台账 ─────────────────────────────────────────
        vm_dir = str(root / "t-bbb")
        marker1 = b.boot_marker(vm_dir)
        p.check("boot_marker_from_fc_sock", marker1.startswith("fc.sock:"), marker1)

        ledger = b.TokenLedger(str(root / "ledger.json"), max_per_boot=1)
        ok1, why1, _ = ledger.claim("t-bbb", marker1)
        p.check("ledger_first_issue_allowed", ok1, why1)
        ok2, why2, _ = ledger.claim("t-bbb", marker1)
        p.check("ledger_second_issue_same_boot_denied", (not ok2) and why2 == "already_issued_this_boot", why2)

        ok3, why3, _ = ledger.claim("t-bbb", marker1 + "-newboot")
        p.check("ledger_new_boot_allowed", ok3, why3)

        # 用独立台账文件 + 独立租户,避免被上面"换 marker"的断言污染状态
        restart_path = str(root / "ledger-restart.json")
        first_boot = b.TokenLedger(restart_path, max_per_boot=1)
        first_boot.claim("t-restart", "m-restart")
        reloaded = b.TokenLedger(restart_path, max_per_boot=1)
        ok4, _, _ = reloaded.claim("t-restart", "m-restart")
        p.check("ledger_survives_restart", not ok4, "台账重启后失忆 → 同一次开机会被发第二枚")

        multi = b.TokenLedger(None, max_per_boot=3)
        for _ in range(3):
            multi.claim("t-x", "m1")
        allowed_after_3, why5, _ = multi.claim("t-x", "m1")
        p.check("ledger_respects_max_per_boot", not allowed_after_3, why5)

        # 并发占额:独立 review 抓出的 TOCTOU,老实现在这里会全部穿透
        import threading as _th
        race = b.TokenLedger(str(root / "ledger-race.json"), max_per_boot=1)
        granted: list[bool] = []
        gl = _th.Lock()
        bar = _th.Barrier(8)

        def _worker():
            bar.wait()
            ok, _, _ = race.claim("t-race", "m1")
            with gl:
                granted.append(ok)

        ths = [_th.Thread(target=_worker) for _ in range(8)]
        [th.start() for th in ths]
        [th.join() for th in ths]
        p.check("ledger_claim_atomic_under_8_threads", granted.count(True) == 1,
                f"{granted.count(True)} 个并发请求都拿到了额度")

        p.check("ledger_unknown_marker_denied", not ledger.claim("t-bbb", "unknown")[0], "unknown marker 被放行")

        # 坏台账默认 fail-closed 并隔离留证
        bad = root / "ledger-bad.json"
        bad.write_text("{broken")
        badledger = b.TokenLedger(str(bad), max_per_boot=1)
        p.check("ledger_corrupt_fail_closed", not badledger.claim("t-y", "m1")[0], "坏台账仍在发证")
        p.check("ledger_corrupt_quarantined", Path(str(bad) + ".corrupt").is_file(), "坏台账没隔离")

        # 签发前复验:同一条 /30 被别的租户接手时必须拒发
        stale = b.load_registry(str(root))
        any_rec = next(iter(stale.values()))
        (Path(any_rec.vm_dir) / "vm.json").write_text(
            json.dumps({"tenant_id": "t-hijack", "vm_num": any_rec.vm_num, "guest_ip": any_rec.guest_ip}))
        p.check("reload_record_detects_tenant_change", b.reload_record(any_rec, str(root)) is None,
                "vm.json 换了租户仍被当成原租户")

        # 另一个【目录】占同一条 /30(slot 复用的真实形态)也必须拒
        two_dirs = Path(tmp) / "dirs"
        write_vm(two_dirs, "t-d1", 21, "10.8.0.2")
        cached_d1 = b.load_registry(str(two_dirs))["10.8.0.1"]
        write_vm(two_dirs, "t-d2", 21, "10.8.0.2")
        p.check("reload_record_detects_other_directory_same_p2p",
                b.reload_record(cached_d1, str(two_dirs)) is None, "新目录占同一条 /30 仍被放行")

        # rp_filter 必须看全量 tap:邻居松也要拒。用独立干净目录建两台 VM
        # (上面的冲突/poison 断言会让主目录只剩一条 tap,那样这条断言没有判别力)
        two = Path(tmp) / "two-taps"
        write_vm(two, "t-p1", 11, "10.9.0.2")   # host 端 10.9.0.1 / tap-vm11
        write_vm(two, "t-p2", 12, "10.9.0.6")   # host 端 10.9.0.5 / tap-vm12
        reg_two = b.load_registry(str(two))
        p.check("two_tap_fixture_ready", len(reg_two) == 2, str(sorted(reg_two)))
        v = b.attest("10.9.0.1", "10.9.0.2", reg_two, "enforce",
                     lambda tap: 1 if tap == "tap-vm11" else 0,
                     rpfilter_rule_lookup=no_rule)
        p.check("attest_denies_when_neighbour_tap_loose",
                (not v.allowed) and v.reason == "spoof_guard_absent", v.reason)
        p.check("rp_filter_reason_hides_tap_names", "tap-vm" not in v.reason, v.reason)
        v = b.attest("10.9.0.1", "10.9.0.2", reg_two, "enforce", lambda tap: 1,
                     rpfilter_rule_lookup=no_rule)
        p.check("attest_allows_when_all_taps_strict", v.allowed, v.reason)

        # ── rp_filter 生效值 = max(conf/all, conf/<iface>),不是 per-iface 那个文件 ──
        # 本轮 BLOCKER 的根。ClawPool host 的 conf/all=2,老实现读 per-tap 读到 1 就报
        # strict → 假绿灯。2026-08-18 真机 netns 实测:all=2+tap=1 时伪造包内核放行。
        conf = Path(tmp) / "rpf-conf"
        for name, value in (("all", 2), ("tap-vm11", 1)):
            (conf / name).mkdir(parents=True)
            (conf / name / "rp_filter").write_text(f"{value}\n")
        p.check("rp_filter_value_is_effective_max",
                b.rp_filter_value("tap-vm11", proc_root=str(conf)) == 2,
                str(b.rp_filter_value("tap-vm11", proc_root=str(conf))))
        p.check("rp_filter_value_missing_tap_is_none",
                b.rp_filter_value("tap-nope", proc_root=str(conf)) is None, "not None")
        ok_g, why_g = b.taps_all_strict(reg_two, proc_root=str(conf))
        p.check("taps_all_strict_false_green_under_all_loose",
                (not ok_g) and why_g == "rp_filter_not_strict", why_g)

        # ── iptables rpfilter 规则识别 ──────────────────────────────────────────
        for label, dump, want in (
            ("present", "-A PREROUTING -i tap+ -p tcp -m tcp --dport 8877 -m rpfilter --invert -j DROP", True),
            ("wrong_port", "-A PREROUTING -i tap+ -p tcp -m tcp --dport 9999 -m rpfilter --invert -j DROP", False),
            ("no_invert", "-A PREROUTING -i tap+ -p tcp -m tcp --dport 8877 -m rpfilter -j DROP", False),
            ("empty", "", False),
        ):
            got, _ = b.rpfilter_match_rules(8877, runner=lambda d=dump: d)
            p.check(f"rpfilter_rule_recognition_{label}", got is want, str(got))
        got, why_u = b.rpfilter_match_rules(8877, runner=lambda: None)
        p.check("rpfilter_rule_unreadable_fail_closed",
                (not got) and why_u == "rpfilter_rules_unreadable", why_u)

        # ── 限流 ──────────────────────────────────────────────────────────────
        rl = b.RateLimiter(per_minute=3)
        results = [rl.allow("10.0.0.2", now=1000.0) for _ in range(4)]
        p.check("rate_limiter_blocks_4th", results[:3] == [True, True, True] and results[3] is False, str(results))
        p.check("rate_limiter_window_slides", rl.allow("10.0.0.2", now=1061.0), "60s 后仍被限")

        # ── token 指纹:日志里绝不出现明文 ────────────────────────────────────
        import uuid as _uuid
        # 不留"长得像凭据"的字面量(CI 的 gitleaks generic-api-key 规则会拦)
        fp = b.token_fingerprint(str(_uuid.uuid5(_uuid.NAMESPACE_DNS, "spire-kit-fixture")))
        p.check("token_fingerprint_is_short_hash", len(fp) == 8 and fp.isalnum(), fp)

    return p.finish()


if __name__ == "__main__":
    sys.exit(main())
