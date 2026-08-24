#!/usr/bin/env python3
"""#547 / #548 的回归守卫:两条结构性生命周期契约。

为什么是 AST 断言而不是行为测试:这两个 bug 的行为侧已经由 issue 里的真机复现证据坐实
(#547 租户 30 分钟删不掉、#548 同 dport 两条 DNAT 指向两个租户),而**修复本身是结构性的** ——
"fail-closed 早退前必须放掉自己的租约"、"stop 分支绝不含 DNAT 删除"。这两句正是回归容易被
悄悄改回去的地方(#548 那段删除循环本来就是后来被拼上去的),所以守结构比复述行为更有效。
`tenant_service.py` 顶部要 boto3,而本仓自测惯例是零依赖(见 test_vm_num_cas_alloc.py),
用 AST 读源码可以两者兼顾。

跑: python3 deploy/lambda/api/test_lifecycle_stop_contract.py
"""

import ast
import pathlib
import sys

SRC = pathlib.Path(__file__).with_name("services") / "tenant_service.py"
PASS = FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"  ✓ {msg}")


def bad(msg):
    global FAIL
    FAIL += 1
    print(f"  ✗ {msg}", file=sys.stderr)


def find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def calls_in(node):
    """node 子树里所有被调用的 attribute 全名(如 lifecycle_fence.release)与裸函数名。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                out.append(f"{f.value.id}.{f.attr}")
            elif isinstance(f, ast.Attribute):
                out.append(f.attr)
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


def const_strs(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


tree = ast.parse(SRC.read_text(encoding="utf-8"))

# ---------- #547 ----------
# REPIN_BACKUP_FAILED 是 fail-closed 早退(强制前置备份失败 → 拒绝继续 rebuild re-pin)。
# 它必须在 return 之前释放自己取得的租约,否则租户 30 分钟内 delete/restart/reset/migrate 全被
# 409 LIFECYCLE_IN_FLIGHT 挡住(真机实测,含删不掉)。
print("== #547 fail-closed 早退必须释放自己的生命周期租约 ==")
fn = find_func(tree, "_rebuild_repin_apply")
if fn is None:
    bad("找不到 _rebuild_repin_apply —— 函数被改名?断言失效,先修本测试")
else:
    repin_blocks = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and any("REPIN_BACKUP_FAILED" == s for s in const_strs(n))
    ]
    if not repin_blocks:
        bad("_rebuild_repin_apply 里找不到 REPIN_BACKUP_FAILED 早退块")
    else:
        # 取最内层那个(if not ok: ... return 502 ...)
        blk = min(repin_blocks, key=lambda n: sum(1 for _ in ast.walk(n)))
        if "lifecycle_fence.release" in calls_in(blk):
            ok("REPIN_BACKUP_FAILED 早退块内调用了 lifecycle_fence.release")
        else:
            bad("REPIN_BACKUP_FAILED 早退块内没有 lifecycle_fence.release —— #547 回归了")

    # 对照:renew_owned 失败分支【不能】释放(那时租约已属别人,释放等于抢锁)。
    superseded = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and any("superseded" in s for s in const_strs(n))
    ]
    if not superseded:
        print("  - 跳过对照:没找到 superseded 分支(措辞变了?)")
    else:
        blk2 = min(superseded, key=lambda n: sum(1 for _ in ast.walk(n)))
        if "lifecycle_fence.release" in calls_in(blk2):
            bad("superseded(renew_owned 失败)分支里出现了 release —— 那会抢掉别人的租约")
        else:
            ok("对照:superseded 分支不释放租约(锁已属别人)")

# ---------- #547 兄弟路径:delete 与 suspend ----------
# 同一个 `_force_backup_sync` 有四个调用者。#547 只修了 rebuild re-pin 那一个,delete 与
# suspend 的 fail-closed 早退仍然扣着自己的租约返 502,而它们的文案都写着 "Retry" ——
# 照文案 retry 会撞 409 LIFECYCLE_IN_FLIGHT(delete 的 :2795 已经把这个病定性过)。
# 守这三处的理由与上面一样:修复是结构性的,回归时只要有人删掉一行就悄悄坏掉。
print("== #547 兄弟路径:delete/suspend 的 stop-vm 之前早退也必须放掉租约 ==")


def sets_fence_release_flag(node):
    """子树里是否有 <ctx>["release_lifecycle_fence_on_error"] = ... 这种下标赋值。

    calls_in 抓不到赋值,delete 侧是直接写 ctx 字典(与 :2805/:2865 同款),所以单独判。
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if (
                isinstance(t, ast.Subscript)
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "release_lifecycle_fence_on_error"
            ):
                return True
    return False


def innermost_if_with(scope, needle):
    """scope 里包含 needle 字面量的最内层 If(与上面 #547 取 blk 的口径一致)。"""
    blocks = [
        n
        for n in ast.walk(scope)
        if isinstance(n, ast.If) and any(needle in s for s in const_strs(n))
    ]
    return min(blocks, key=lambda n: sum(1 for _ in ast.walk(n))) if blocks else None


# --- delete 侧:pre-delete backup failed 早退 ---
dfn = find_func(tree, "_delete_tenant_inner")
if dfn is None:
    bad("找不到 _delete_tenant_inner —— 断言失效,先修本测试")
else:
    dblk = innermost_if_with(dfn, "pre-delete backup failed")
    if dblk is None:
        bad("_delete_tenant_inner 里找不到 pre-delete backup failed 早退块")
    elif sets_fence_release_flag(dblk):
        ok("delete 的 pre-delete backup failed 早退块置了 release_lifecycle_fence_on_error")
    else:
        bad(
            "delete 的 pre-delete backup failed 早退块没置 "
            "release_lifecycle_fence_on_error —— #547 在 delete 侧回归了"
        )

# --- suspend 侧:行序不变量 ---
# 判据不是"文案里有没有 Retry",而是【结构】:放行只允许出现在第一个破坏性命令
# (`_stop_ok, _stop_rc = ssm_dispatch._ssm_run(...)`)【之前】。用行号守这条,措辞怎么改都
# 不影响;而一旦有人把放行挪到 stop-vm 之后(那会让 restore 与在途 stop 交错),立刻打红。
sfn = find_func(tree, "_tenant_suspend")
if sfn is None:
    bad("找不到 _tenant_suspend —— 断言失效,先修本测试")
else:
    stop_lns = [
        n.lineno
        for n in ast.walk(sfn)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Tuple)
            and any(isinstance(e, ast.Name) and e.id == "_stop_ok" for e in t.elts)
            for t in n.targets
        )
    ]
    rel_lns = [
        n.lineno
        for n in ast.walk(sfn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_release_fence_no_host_work"
    ]
    if not stop_lns:
        bad("_tenant_suspend 里找不到 `_stop_ok, _stop_rc = ...` —— 分界线没了,先修本测试")
    elif not rel_lns:
        bad("_tenant_suspend 里没有任何 _release_fence_no_host_work 调用 —— 8 个 502 全扣租约")
    else:
        stop_ln = min(stop_lns)
        late = [ln for ln in rel_lns if ln > stop_ln]
        if late:
            bad(
                f"_release_fence_no_host_work 出现在 stop-vm(:{stop_ln})之后的 {late} 行 —— "
                "那时命令已下发、结果未知,放掉围栏会让 restore 与在途 stop 交错"
            )
        else:
            ok(f"suspend 的 {len(rel_lns)} 处放行全部在 stop-vm(:{stop_ln})之前")
        if len(rel_lns) < 3:
            bad(
                f"suspend 的 stop-vm 之前有三个可证明 host 侧未动的 502(备份确定失败 / "
                f"备份抛异常 / 无 backup_key),但只有 {len(rel_lns)} 处放行"
            )
        else:
            ok("suspend 的三个 stop-vm 之前的 502 都放行了")

    # 对照:vm_left_paused 那支【不能】放 —— 它是唯一 host 侧真动过且没回滚的
    # (VM 被留在 Paused),且有意保留 status=suspending 让 stuck-lifecycle 告警看得见。
    pblk = innermost_if_with(sfn, "left the VM PAUSED")
    if pblk is None:
        print("  - 跳过对照:没找到 vm_left_paused 分支(措辞变了?)")
    elif "_release_fence_no_host_work" in calls_in(pblk):
        bad("vm_left_paused 分支放掉了围栏 —— 那会让 restore/start 撞上一台冻住的 VM")
    else:
        ok("对照:vm_left_paused 分支不放围栏(VM 真的还冻着)")

    # 桥面检查:kwarg 定义了还得真传下去,否则 _lifecycle_ctx 恒为 None、上面三处全是空操作。
    if "_lifecycle_ctx" not in [a.arg for a in sfn.args.args]:
        bad("_tenant_suspend 没有 _lifecycle_ctx 形参 —— 放行无从写回 ctx")
    else:
        passed = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_tenant_suspend"
            and any(k.arg == "_lifecycle_ctx" for k in n.keywords)
            for n in ast.walk(tree)
        )
        if passed:
            ok("_tenant_suspend 的调用点真的传了 _lifecycle_ctx(桥墩+桥面都在)")
        else:
            bad("_tenant_suspend 有 _lifecycle_ctx 形参但调用点没传 —— 三处放行全是空操作")

# ---------- #548 ----------
# route_ops.py 的 R2.3:only DELETE reclaims, STOP never does。控制面曾把一条
# `iptables -D PREROUTING` 删除循环拼在 stop-vm.sh 后面,打破了端口模型的不变量:
# 位图由活规则重建 → 端口被判空闲 → 发给下一个租户 → 停机租户 start 时加回 → 同 dport 两条 DNAT。
print("== #548 stop 分支绝不含 DNAT 删除(only DELETE reclaims) ==")
handler = None
for cand in ("_apply_lifecycle_action", "lifecycle_action", "_do_lifecycle"):
    handler = find_func(tree, cand)
    if handler is not None:
        break
stop_branches = []
scope = handler if handler is not None else tree
for n in ast.walk(scope):
    if not isinstance(n, ast.If):
        continue
    test = n.test
    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == "action"
            and any(isinstance(c, ast.Constant) and c.value == "stop" for c in test.comparators)):
        stop_branches.append(n)
if not stop_branches:
    bad("找不到 `action == \"stop\"` 分支 —— 断言失效,先修本测试")
else:
    bodies = []
    for br in stop_branches:
        bodies.extend(br.body)          # 只看 stop 自己的 body,不含 elif 链上的其它动作
    used = []
    for b in bodies:
        used.extend(calls_in(b))
    if "_dnat_remove_all_cmd" in used:
        bad("stop 分支仍在调 _dnat_remove_all_cmd —— #548 回归了(stop 不得回收端口/DNAT)")
    else:
        ok("stop 分支不含 _dnat_remove_all_cmd")
    joined = " ".join(s for b in bodies for s in const_strs(b))
    if "-D PREROUTING" in joined:
        bad("stop 分支里出现字面量 `-D PREROUTING` —— 绕过 helper 也算回归")
    else:
        ok("stop 分支无 `-D PREROUTING` 字面量")

# 对照:delete 与 suspend 仍然应该回收(否则端口/DNAT 永久泄漏)。
callers = [n.name for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and "_dnat_remove_all_cmd" in calls_in(n)]
if callers:
    ok(f"对照:回收仍存在于 {sorted(set(callers))}(delete/suspend 路径应保留)")
else:
    bad("全仓已无 _dnat_remove_all_cmd 调用 —— 端口/DNAT 再也不会被回收,泄漏")

print(f"\n== totals: {PASS} pass / {FAIL} fail ==")
sys.exit(1 if FAIL else 0)
