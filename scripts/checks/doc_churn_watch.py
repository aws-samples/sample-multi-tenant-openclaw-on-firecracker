#!/usr/bin/env python3
"""每日 churn 巡检——只读报告,不改任何文档。

瀑布流快速开发,功能半衰期 3-6 天。哪些代码文件最近改得最猛(高波动区),
引用它们的 fact 文档最可能已经过期。本脚本每天算一次热点,列出"该优先核对"的
文档清单,让人/AI 一眼看到风险,不自动改文档(有损写入交人判断)。

用法: doc_churn_watch.py [--since 1.day] [--top 15]
输出: 热点代码文件(按改动次数) + 每个热点被哪些 fact 文档引用。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

REPO = (
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    or os.getcwd()
)

DOC_GLOBS = ["*.md", "*.svg"]
DOC_EXCLUDE = re.compile(r"(^|/)(archive|node_modules)/")
CODE_EXT = (".py", ".sh", ".ts", ".js", ".tsx", ".zig", ".go")
REF_RE = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh|ts|js|tsx|jsx|zig|go|java)):\d+"
)


def _run(args: list[str]) -> str:
    # -c core.quotepath=false: 中文/空格文件名不被 git 加引号转义,否则路径解析出错
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args[1:]] if args[0] == "git" else args,
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout


def hot_files(since: str, top: int) -> list[tuple[str, int]]:
    names = _run(
        ["git", "log", f"--since={since}", "--name-only", "--pretty=format:"]
    ).split()
    c = Counter(n for n in names if n.endswith(CODE_EXT))
    return c.most_common(top)


def doc_refs_index() -> dict[str, list[str]]:
    """basename -> 引用它的 fact 文档列表。"""
    docs = [
        d
        for d in _run(["git", "ls-files", *DOC_GLOBS]).splitlines()
        if not DOC_EXCLUDE.search(d)
    ]
    idx: dict[str, list[str]] = defaultdict(list)
    for d in docs:
        text = open(os.path.join(REPO, d), encoding="utf-8", errors="ignore").read()
        bases = {os.path.basename(m.group(1)) for m in REF_RE.finditer(text)}
        for b in bases:
            idx[b].append(d)
    return idx


def main() -> int:
    since = "1.day"
    top = 15
    args = sys.argv[1:]
    if "--since" in args:
        since = args[args.index("--since") + 1]
    if "--top" in args:
        top = int(args[args.index("--top") + 1])

    hot = hot_files(since, top)
    if not hot:
        print(f"最近 {since} 无代码改动,无热点。")
        return 0

    refs = doc_refs_index()
    print(f"== churn 巡检: 最近 {since} 改动最频繁的代码文件 (top {top}) ==")
    flagged = 0
    for path, n in hot:
        docs = refs.get(os.path.basename(path), [])
        tag = f"  ← 被 {len(docs)} 篇 fact 文档引用,务必核对" if docs else ""
        print(f"  {n:3d}次  {path}{tag}")
        for d in docs:
            print(f"           核: {d}")
            flagged += 1
    print(
        f"\n{flagged} 处文档引用了本周期高波动代码,建议 doc_freshness.py check 逐一核对。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
