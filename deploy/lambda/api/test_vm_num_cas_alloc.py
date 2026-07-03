#!/usr/bin/env python3
"""验证 create_tenant 的 vm_num 分配 CAS 修复:并发抢同一 host 不再撞槽。

不依赖真实 DynamoDB —— 用一个线程安全的 FakeTable 精确模拟 DDB 的条件写语义
(ConditionExpression 不满足时抛 ConditionalCheckFailedException),复现修复前
后的行为差异。这是对 handler.py _reserve_slot CAS 逻辑的隔离验证。
"""

import threading
import concurrent.futures


class ConditionalCheckFailed(Exception):
    pass


class FakeHostsTable:
    """模拟 DDB 单 host item + 原子条件 update_item。"""

    def __init__(self, total_vcpu=96, total_mem_mb=768000):
        self.item = {
            "instance_id": "i-host",
            "next_vm_num": 1,
            "used_vcpu": 0,
            "used_mem_mb": 0,
            "vm_count": 0,
            "total_vcpu": total_vcpu,
            "total_mem_mb": total_mem_mb,
        }
        self._lock = threading.Lock()  # DDB 单 item 写是串行原子的

    def cas_reserve(self, expected_next, vcpu, mem_mb, cap_v, cap_m):
        """对应 handler.py _reserve_slot 的条件更新。返回 claimed vm_num 或抛异常。"""
        with self._lock:  # DDB 保证单 item 条件写原子
            it = self.item
            # ConditionExpression: next_vm_num == expected AND used+delta <= cap
            if it["next_vm_num"] != expected_next:
                raise ConditionalCheckFailed()
            if it["used_vcpu"] + vcpu > cap_v + vcpu:  # cap_v = allocatable - vcpu
                raise ConditionalCheckFailed()
            if it["used_mem_mb"] + mem_mb > cap_m + mem_mb:
                raise ConditionalCheckFailed()
            it["used_vcpu"] += vcpu
            it["used_mem_mb"] += mem_mb
            it["vm_count"] += 1
            it["next_vm_num"] += 1
            return it["next_vm_num"] - 1


def reserve_slot_FIXED(table, vcpu, mem_mb):
    """复刻 handler.py 修复后的 _reserve_slot + 重试。"""
    CPU_OVERCOMMIT, MEM_OVERCOMMIT = 1.0, 1.0
    for attempt in range(8):
        snap = dict(table.item)  # 模拟 _find_host 读到的快照
        expected = snap["next_vm_num"]
        cap_v = int(snap["total_vcpu"] * CPU_OVERCOMMIT) - vcpu
        cap_m = int(snap["total_mem_mb"] * MEM_OVERCOMMIT) - mem_mb
        try:
            return table.cas_reserve(expected, vcpu, mem_mb, cap_v, cap_m)
        except ConditionalCheckFailed:
            continue  # 重选/重试
    return None


def alloc_BROKEN(table, vcpu, mem_mb):
    """复刻修复前:读 next_vm_num(非原子快照)→ 用它算 vm_num → 之后才无条件
    绝对赋值。读和写之间是非原子窗口 —— 真实 Lambda 里这中间还隔着 put_item 等
    操作(分散到不同并发执行)。用一个 time.sleep 模拟这个窗口让竞态显现。"""
    import time as _t

    snap = dict(table.item)  # 读快照(对应 host = _find_host(...))
    vm_num = snap["next_vm_num"]  # 用快照里的 next_vm_num
    _t.sleep(0.002)  # 读→写窗口:并发请求都已读到同一个 vm_num
    with table._lock:
        table.item["used_vcpu"] += vcpu
        table.item["used_mem_mb"] += mem_mb
        table.item["vm_count"] += 1
        table.item["next_vm_num"] = vm_num + 1  # 绝对赋值,丢并发增量
    return vm_num


def run(label, fn, n=50):
    table = FakeHostsTable()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(fn, table, 1, 2048) for _ in range(n)]
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            if r is not None:
                results.append(r)
    uniq = set(results)
    dupes = len(results) - len(uniq)
    print(f"[{label}] {n} 并发建租户")
    print(f"  分配成功: {len(results)}  唯一 vm_num: {len(uniq)}  撞槽(重复): {dupes}")
    print(
        f"  最终 next_vm_num: {table.item['next_vm_num']}  used_vcpu: {table.item['used_vcpu']}"
    )
    print(
        f"  账本一致(next_vm_num-1 == 成功数 == used_vcpu): "
        f"{table.item['next_vm_num'] - 1 == len(results) == table.item['used_vcpu']}"
    )
    return dupes


print("=" * 60)
broken_dupes = run("修复前 BROKEN", alloc_BROKEN)
print("-" * 60)
fixed_dupes = run("修复后 FIXED (CAS)", reserve_slot_FIXED)
print("=" * 60)
assert fixed_dupes == 0, f"FIXED 仍有撞槽 {fixed_dupes}!"
print(f"\n结论: 修复前撞槽 {broken_dupes} 次, 修复后撞槽 {fixed_dupes} 次。")
print("CAS 乐观锁验证通过 ✓" if fixed_dupes == 0 else "✗ 修复无效")
