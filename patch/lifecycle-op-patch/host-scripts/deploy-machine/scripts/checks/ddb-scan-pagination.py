#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#432 —— 每一处 DynamoDB `Scan` 都必须翻页。这道门让「忘记翻页」不可能悄悄合进来。

## 为什么机械化而不是写进文档

本仓**早就**把这条纪律写下来了 —— `core/scheduling.py::_registered_host_count` 的注释原话:

> a DynamoDB Scan returns at most 1MB per call, and openclaw-hosts accumulates `deleted`
> rows over its lifetime … pagination is required even though the match set is small.

`scaler/handler.py::_has_pending_tenants` 也被那段注释引为「同款纪律」。然而 2026-08-21 的
审计发现:**42 个 Scan 调用点里有 17 个没照做**。文档拦不住,所以改成会红的门。

## 判据与失效形态

DDB 单次 `Scan` 最多返回 1 MB,`FilterExpression` 在那 1 MB 读【之后】才过滤。所以决定页数的
是**全表字节数**,不是命中数 —— 「匹配集合很小」这个直觉正是踩坑的原因。实测(apse1):

  · `openclaw-hosts`  : 39 行 / 15,752 字节 = 403 字节/行,其中 33 行是 `deleted`
                        → 1 MB ≈ 2,601 行;死行只增不减(每台 terminate 留一行)
  · `openclaw-tenants`: 6,790 行 / 2,829,168 字节 → **已超 1 MB 近三倍**

后果全是**静默看错**,不是报错:选点看不见后页 host → 有容量却报 unplaced;TTL 扫不到后页
租户 → 永不过期且持续计费;健康检查扫不到 → 坏了没人发现;疏散扫不到 → 租户被留在死机器上。

## 判定方式(按调用点结构,不按距离)

用 AST 定位每个 `.scan(...)` 调用点,逐个判定。第一版审计用「scan 之后 40 行内出现
LastEvaluatedKey」,对 `demote_stale_hosts` 给了假阳性 —— 它的翻页设置在 scan 之【前】、
`LastEvaluatedKey` 在循环末尾,两个都落在窗口外。按距离判不如按结构判。

合规有四种形态,都接受:
  ① 调用点显式带 `ExclusiveStartKey`;
  ② 调用点位于引用 `LastEvaluatedKey` 的循环内;
  ③ 调用结果的变量在同一函数内读取 `LastEvaluatedKey` 并作为游标返回;
  ④ 用 `# scan-single-page-ok: <理由>` 显式标注的刻意单页 scan(留痕,给 legacy
     契约等确实不能翻页的场景)。
推荐的新代码仍走 `ddb_scan.scan_all(...)`(`kwargs` 逐页透传,漏传 filter 这类错从根上没有);
不把既有的手写翻页强行改写,它们是对的,改动只会放大 diff 与回归面(铁律 #2)。

用法:
    python3 scripts/checks/ddb-scan-pagination.py [--repo-root .]
    python3 scripts/checks/ddb-scan-pagination.py --selfcheck
退出:0=全部翻页;1=有裸 Scan;2=无法完成检查(fail-closed)。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import List, Tuple

# 不扫的路径:测试(mock 表本来就不翻页)、样例参考、第三方、发布产物。
SKIP_PARTS = (
    "/tests/",
    "/.git/",
    "/opensource/",
    "claw-patch-skill",          # patch kit 的历史样例快照,不是生产代码
    "/node_modules/",
    "/.venv/",
    "/site-packages/",
)

# 只看这些目录 —— 生产 Lambda 与运维脚本。
SCAN_ROOTS = ("deploy/lambda", "engineering/tooling", "cli")

# 硬下限:扫到的 scan 调用点少于这个数,说明检查根本没生效(路径写错/目录挪了),
# 必须 fail-closed。一个恒绿的门比没有门更糟 —— 它会被当成「已达标」的证据。
MIN_EXPECTED_SITES = 20


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_LOOP_NODES = (ast.While, ast.For, ast.AsyncFor)
_SINGLE_PAGE_OK_MARKER = "# scan-single-page-ok:"


def _is_table_scan(call: ast.Call, source: str) -> bool:
    """是否是对 DDB 表句柄的裸 `.scan(...)` 调用。

    receiver 文本含 `_table`(本仓表句柄的命名约定),或 receiver 就叫 `table` 时纳入。
    这样不会把 `s3.scan`、`re.scan` 之类误判进来。

    不追求识别 `getattr(table, "scan")` 或任意别名(比如
    `t = clients.hosts_table` 之后 `t.scan(...)`)——那属于本门的已知边界,
    写在这里而不是假装没有。
    """
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "scan":
        return False
    receiver = ast.get_source_segment(source, call.func.value)
    if receiver is None:
        receiver = ast.unparse(call.func.value)
    receiver = receiver.strip()
    return receiver == "table" or "_table" in receiver


def _parent_map(tree: ast.AST) -> dict:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _enclosing_function(node: ast.AST, parents: dict):
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, _FUNCTION_NODES):
            return parent
        parent = parents.get(parent)
    return None


def _references_last_evaluated_key(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "LastEvaluatedKey":
            return True
        if (
            isinstance(child, ast.Constant)
            and child.value == "LastEvaluatedKey"
        ):
            return True
    return False


def _inside_paginated_loop(call: ast.Call, parents: dict) -> bool:
    parent = parents.get(call)
    while parent is not None:
        if isinstance(parent, _FUNCTION_NODES):
            return False
        if (
            isinstance(parent, _LOOP_NODES)
            and _references_last_evaluated_key(parent)
        ):
            return True
        parent = parents.get(parent)
    return False


def _target_names(target: ast.AST) -> List[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: List[str] = []
        for element in target.elts:
            names.extend(_target_names(element))
        return names
    return []


def _assigned_names(call: ast.Call, parents: dict) -> List[str]:
    parent = parents.get(call)
    if isinstance(parent, ast.Assign) and parent.value is call:
        names: List[str] = []
        for target in parent.targets:
            names.extend(_target_names(target))
        return names
    if isinstance(parent, ast.AnnAssign) and parent.value is call:
        return _target_names(parent.target)
    if isinstance(parent, ast.NamedExpr) and parent.value is call:
        return _target_names(parent.target)
    return []


def _reads_cursor_from(node: ast.AST, variable: str) -> bool:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == variable
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "LastEvaluatedKey"
    ):
        return True
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == variable
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "LastEvaluatedKey"
    )


def _returns_cursor(
    call: ast.Call, function: ast.AST | None, parents: dict
) -> bool:
    if function is None:
        return False
    variables = _assigned_names(call, parents)
    if not variables:
        return False
    for node in ast.walk(function):
        if _enclosing_function(node, parents) is not function:
            continue
        if any(_reads_cursor_from(node, variable) for variable in variables):
            return True
    return False


def _is_paginated(
    call: ast.Call, function: ast.AST | None, parents: dict, line_text: str
) -> bool:
    if _SINGLE_PAGE_OK_MARKER in line_text:
        return True
    if any(kw.arg == "ExclusiveStartKey" for kw in call.keywords):
        return True
    return _inside_paginated_loop(call, parents) or _returns_cursor(
        call, function, parents
    )


def _audit_source(source: str) -> Tuple[List[Tuple[int, str]], int]:
    tree = ast.parse(source)
    parents = _parent_map(tree)
    source_lines = source.splitlines()
    bad: List[Tuple[int, str]] = []
    total = 0
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not _is_table_scan(call, source):
            continue
        total += 1
        function = _enclosing_function(call, parents)
        line_index = call.lineno - 1
        line_text = (
            source_lines[line_index]
            if 0 <= line_index < len(source_lines)
            else ""
        )
        if not _is_paginated(call, function, parents, line_text):
            bad.append((call.lineno, function.name if function else "<module>"))
    return bad, total


def audit(root: Path) -> Tuple[List[str], int]:
    """返回 (违规清单, 检查到的 `.scan(...)` 调用点总数)。"""
    bad: List[str] = []
    total = 0
    for sub in SCAN_ROOTS:
        base = root / sub
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            sp = str(p)
            if any(x in sp for x in SKIP_PARTS):
                continue
            if (
                p.name == "ddb_scan.py"
                or p.name.startswith("test_")
                or p.name.endswith("_test.py")
            ):
                continue
            try:
                src = p.read_text(encoding="utf-8")
                file_bad, file_total = _audit_source(src)
            except (OSError, SyntaxError) as e:
                print(f"FATAL 读不动/解析不了 {p}: {e}")
                sys.exit(2)
            total += file_total
            rel = p.relative_to(root)
            for lineno, function_name in file_bad:
                bad.append(f"{rel}:{lineno} {function_name}()")
    return bad, total


def _selfcheck() -> int:
    """反向验证:裸 Scan 必须被抓到,显式合规形态必须被放过。

    没有这一步,一个恒绿的门会被当成「全仓已达标」的证据 —— 那比没有门更糟。
    """
    cases = [
        ("裸 scan(应抓)", '''
def f():
    return tenants_table.scan(FilterExpression="a = :b").get("Items", [])
''', True),
        ("显式单页标记(应放过)", '''
def f():
    return hosts_table.scan().get("Items", [])  # scan-single-page-ok: legacy contract
''', False),
        ("无显式单页标记(应抓)", '''
def f():
    return hosts_table.scan().get("Items", [])
''', True),
        ("走 scan_all(应放过)", '''
def f():
    return ddb_scan.scan_all(tenants_table, FilterExpression="a = :b")
''', False),
        ("自己翻页(应放过)", '''
def f():
    out, k = [], None
    while True:
        kw = {}
        if k:
            kw["ExclusiveStartKey"] = k
        page = tenants_table.scan(**kw)
        out += page.get("Items", [])
        k = page.get("LastEvaluatedKey")
        if not k:
            return out
''', False),
        ("只有 LastEvaluatedKey 没有 ExclusiveStartKey(应抓 —— 读了游标却没用它)", '''
def f():
    page = hosts_table.scan().get("Items", [])
    _ = "LastEvaluatedKey"
    return page
''', True),
        ("同函数内翻页循环不能掩盖另一处裸 scan(应抓)", '''
def f():
    out, k = [], None
    while True:
        kw = {}
        if k:
            kw["ExclusiveStartKey"] = k
        page = hosts_table.scan(**kw)
        out += page.get("Items", [])
        k = page.get("LastEvaluatedKey")
        if not k:
            break
    migrating = tenants_table.scan(FilterExpression="#s = :m").get("Items", [])
    return out, migrating
''', True),
        ("非表对象的 scan(不应抓)", '''
def f():
    return some_scanner.scan(target="x")
''', False),
    ]
    bad = 0
    for name, code, should_flag in cases:
        violations, _ = _audit_source(code)
        flagged = bool(violations)
        if flagged != should_flag:
            print(f"  ✗ {name}: 期望{'抓' if should_flag else '放过'},实际"
                  f"{'抓了' if flagged else '放过了'}")
            bad += 1
        else:
            print(f"  ✓ {name}")
    print(f"\n{'✓ selfcheck 全过' if not bad else f'✗ selfcheck 有 {bad} 条问题'}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="#432 DDB Scan 翻页门")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return _selfcheck()

    root = Path(a.repo_root).resolve()
    bad, total = audit(root)
    if total < MIN_EXPECTED_SITES:
        print(
            f"FATAL 只扫到 {total} 个 DDB scan 调用点(下限 {MIN_EXPECTED_SITES})——"
            "检查大概率没生效(SCAN_ROOTS 路径过期?),fail-closed。"
        )
        return 2
    if bad:
        print(f"✗ {len(bad)} 处 DynamoDB Scan 没有翻页(共检查 {total} 处):")
        for b in bad:
            print(f"    {b}")
        print(
            "\n修法:改走 `ddb_scan.scan_all(table, **kwargs)`(api 包用 "
            "`core.ddb_scan`,health_check / scaler 各包自带一份 —— 三个 Lambda 独立打包,"
            "无法共享模块)。或自己用 ExclusiveStartKey/LastEvaluatedKey 翻完。\n"
            "为什么不能不翻页:DDB 单次 Scan 上限 1MB,FilterExpression 在那 1MB 读【之后】"
            "才过滤 —— 决定页数的是全表字节数,不是命中数。openclaw-tenants 实测已超 1MB 近三倍。"
        )
        return 1
    print(f"✓ {total} 处 DynamoDB Scan 全部翻页")
    return 0


if __name__ == "__main__":
    sys.exit(main())
