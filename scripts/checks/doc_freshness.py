#!/usr/bin/env python3
"""fact 文件漂移检测(doc-freshness)——纯 stdlib + git,零依赖不烧 token。

痛点:瀑布流快速开发,功能半衰期 3-6 天,AI grep 到旧文档/旧代码当真相。
本工具让过期文档"自己喊死":扫 fact 文件里已写的 file:line 引用做锚点,
记目标行内容指纹到 lock;代码一改指纹就变,check 报 STALE 挡 merge。

三个确定性信号(参考 Dosu freshness / Fiberplane Drift):
  1. 锚点漂移:file:line 死链 / 行号越界 / 目标行内容指纹变了(STALE)
  2. frontmatter TTL:文件头 verified_until / verified_against 过期
  3. git age delta:引用的代码文件比文档新(可疑,弱信号只扣分不挡)

子命令:
  link  [files...]  扫锚点,把 file:line + 目标行内容指纹写进 lock(建立/刷新基线)
  check [files...]  重算,报 STALE/死链/越界/TTL 过期;有硬漂移退出码 1(CI 门用)
  score [files...]  输出每文件 0-100 保鲜分 + 汇总(巡检/趋势用)

不带 files 时用默认 fact 清单(FACT_GLOBS)。lock 放 docs/.doc-anchors.lock。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

REPO = (
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    or os.getcwd()
)

LOCK_PATH = os.path.join(REPO, "docs", ".doc-anchors.lock")

# 默认重点保护的 fact 文件(相对仓库根的 glob)。新增 fact 文件加这里。
FACT_GLOBS = [
    "README.md",
    "docs/**/*.md",
]

# file:line[-line] 引用。只认代码扩展名,避免把版本号/端口误当引用。
ANCHOR_RE = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh|ts|js|tsx|jsx|zig|go|java))"
    r":(\d+)(?:\s*[-–]\s*(\d+))?"
)
# frontmatter 里的 TTL 契约(两种写法任一)
TTL_RE = re.compile(r"^\s*verified_until:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.M)
SHA_RE = re.compile(r"^\s*verified_against:\s*([0-9a-f]{7,40})", re.M)


def _run(args: list[str]) -> str:
    # -c core.quotepath=false: 中文/空格文件名不被 git 加引号转义,否则路径解析出错
    if args and args[0] == "git":
        args = ["git", "-c", "core.quotepath=false", *args[1:]]
    return subprocess.run(args, capture_output=True, text=True, cwd=REPO).stdout


def _basename_index() -> dict[str, list[str]]:
    """git 跟踪文件的 basename -> 路径列表,供裸文件名(ha_edge.py)解析真实路径。"""
    idx: dict[str, list[str]] = defaultdict(list)
    for p in _run(["git", "ls-files"]).splitlines():
        idx[os.path.basename(p)].append(p)
    return idx


def resolve_path(path: str, idx: dict[str, list[str]]) -> str | None:
    """把引用里的路径解析成仓库内真实文件;不存在返回 None(死链)。"""
    if os.path.isfile(os.path.join(REPO, path)):
        return path
    cands = [
        c
        for c in idx.get(os.path.basename(path), [])
        if os.path.isfile(os.path.join(REPO, c))
    ]
    if not cands:
        return None
    # 引用带目录时(deploy/stack.py),优先路径后缀精确匹配,避免撞同名文件
    suffix = [c for c in cands if c == path or c.endswith("/" + path)]
    if suffix:
        cands = suffix
    if len(cands) == 1:
        return cands[0]
    # 裸文件名多候选(handler.py 有多个):选行数最多的真身,而非同名小文件
    return max(
        cands,
        key=lambda c: sum(
            1 for _ in open(os.path.join(REPO, c), encoding="utf-8", errors="ignore")
        ),
    )


def fingerprint(real: str, l1: int, l2: int | None) -> str:
    """目标行范围的内容指纹。normalize(strip 每行首尾空格)后 sha1,
    重排版/缩进变化不误报,内容变了才变。
    usedforsecurity=False:仅做内容变化检测,非密码学安全用途(bandit B324)。"""
    with open(os.path.join(REPO, real), encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    hi = l2 or l1
    chunk = [ln.strip() for ln in lines[l1 - 1 : hi]]  # 1-indexed
    return hashlib.sha1("\n".join(chunk).encode(), usedforsecurity=False).hexdigest()[
        :16
    ]


def parse_anchors(text: str) -> list[tuple[str, int, int | None]]:
    seen: list[tuple[str, int, int | None]] = []
    for m in ANCHOR_RE.finditer(text):
        seen.append(
            (m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        )
    return seen


# 全仓彻查模式排除的路径(历史归档 + 外部代码,本就不该核当前性)
ALL_EXCLUDE = re.compile(r"(^|/)(archive|node_modules)/")


def expand_facts(files: list[str]) -> list[str]:
    if files == ["--all"]:
        docs = _run(["git", "ls-files", "*.md", "*.svg"]).splitlines()
        return [d for d in docs if not ALL_EXCLUDE.search(d)]
    if files:
        return [f for f in files if os.path.isfile(os.path.join(REPO, f))]
    out: list[str] = []
    for g in FACT_GLOBS:
        out.extend(sorted(glob.glob(os.path.join(REPO, g))))
    return [os.path.relpath(p, REPO) for p in out]


def _last_commit_epoch(path: str) -> int:
    out = _run(["git", "log", "-1", "--format=%ct", "--", path]).strip()
    return int(out) if out.isdigit() else 0


def scan_file(doc: str, idx: dict[str, list[str]], lock: dict) -> dict:
    """扫一个 fact 文件,返回它的锚点状态 + 信号。"""
    text = open(os.path.join(REPO, doc), encoding="utf-8", errors="ignore").read()
    anchors = parse_anchors(text)
    doc_epoch = _last_commit_epoch(doc)
    baseline = lock.get(doc, {}).get("anchors", {})

    dead, oob, stale, ok, newer = [], [], [], [], []
    for path, l1, l2 in anchors:
        key = f"{path}:{l1}" + (f"-{l2}" if l2 else "")
        real = resolve_path(path, idx)
        if not real:
            dead.append(key)
            continue
        nlines = sum(
            1 for _ in open(os.path.join(REPO, real), encoding="utf-8", errors="ignore")
        )
        if (l2 or l1) > nlines:
            oob.append(f"{key} (文件仅{nlines}行)")
            continue
        sig = fingerprint(real, l1, l2)
        base_sig = baseline.get(key)
        if base_sig and base_sig != sig:
            stale.append(key)
        else:
            ok.append(key)
        # git age delta:代码比文档新 = 弱可疑信号
        if doc_epoch and _last_commit_epoch(real) > doc_epoch:
            newer.append(key)

    # frontmatter TTL(取文件头 2KB)
    head = text[:2048]
    ttl_expired = None
    m = TTL_RE.search(head)
    if m:
        today = _run(["git", "log", "-1", "--format=%cs"]).strip() or ""
        # 用 HEAD 提交日期做"现在",避免脚本里禁用的 Date.now()/系统时钟不确定
        ttl_expired = m.group(1) < today if today else None

    return {
        "anchors": len(anchors),
        "dead": dead,
        "oob": oob,
        "stale": stale,
        "ok": ok,
        "newer": newer,
        "ttl": m.group(1) if m else None,
        "ttl_expired": ttl_expired,
        "sha_pin": SHA_RE.search(head).group(1) if SHA_RE.search(head) else None,
    }


def score(st: dict) -> int:
    """0-100 保鲜分。硬漂移(死链/越界/STALE/TTL 过期)重扣,age delta 轻扣。"""
    n = max(st["anchors"], 1)
    s = 100
    s -= 100 * len(st["dead"]) // n  # 死链最重
    s -= 80 * len(st["oob"]) // n
    s -= 70 * len(st["stale"]) // n
    s -= 20 * len(st["newer"]) // n  # 弱信号
    if st["ttl_expired"]:
        s -= 30
    return max(s, 0)


def load_lock() -> dict:
    return json.load(open(LOCK_PATH)) if os.path.isfile(LOCK_PATH) else {}


def cmd_link(files: list[str]) -> int:
    idx = _basename_index()
    lock = load_lock()
    for doc in expand_facts(files):
        text = open(os.path.join(REPO, doc), encoding="utf-8", errors="ignore").read()
        anchors_sig = {}
        for path, l1, l2 in parse_anchors(text):
            real = resolve_path(path, idx)
            if not real:
                continue
            nlines = sum(
                1
                for _ in open(
                    os.path.join(REPO, real), encoding="utf-8", errors="ignore"
                )
            )
            if (l2 or l1) > nlines:
                continue
            key = f"{path}:{l1}" + (f"-{l2}" if l2 else "")
            anchors_sig[key] = fingerprint(real, l1, l2)
        lock[doc] = {"anchors": anchors_sig}
        print(f"  linked {doc}: {len(anchors_sig)} 锚点")
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    json.dump(lock, open(LOCK_PATH, "w"), indent=2, ensure_ascii=False, sort_keys=True)
    print(f"lock 已写 {os.path.relpath(LOCK_PATH, REPO)}")
    return 0


def cmd_check(files: list[str]) -> int:
    idx = _basename_index()
    lock = load_lock()
    hard = 0
    for doc in expand_facts(files):
        st = scan_file(doc, idx, lock)
        sc = score(st)
        bad = st["dead"] or st["oob"] or st["stale"] or st["ttl_expired"]
        flag = "STALE" if bad else "ok"
        print(f"[{flag}] {doc}  保鲜分={sc}  锚点={st['anchors']}")
        for k in st["dead"]:
            print(f"    死链: {k}(引用的代码文件不存在)")
        for k in st["oob"]:
            print(f"    越界: {k}")
        for k in st["stale"]:
            print(f"    内容变了(指纹漂移,需核对并 link 刷新): {k}")
        if st["ttl_expired"]:
            print(f"    TTL 过期: verified_until={st['ttl']}(需重新核对内容后更新)")
        if st["newer"] and not bad:
            print(f"    提示: {len(st['newer'])} 处引用的代码比本文档新,建议核对")
        if bad:
            hard += 1
    if hard:
        print(
            f"\n{hard} 个 fact 文件有硬漂移。核对内容后 `doc_freshness.py link <文件>` 刷新基线。"
        )
        return 1
    print("\n全部 fact 文件新鲜。")
    return 0


def cmd_score(files: list[str]) -> int:
    idx = _basename_index()
    lock = load_lock()
    docs = expand_facts(files)
    total = 0
    for doc in docs:
        st = scan_file(doc, idx, lock)
        sc = score(st)
        total += sc
        print(f"{sc:3d}  {doc}")
    if docs:
        print(f"---\n平均保鲜分 {total // len(docs)} / 100 ({len(docs)} 个 fact 文件)")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("link", "check", "score"):
        print(__doc__)
        return 2
    cmd, files = sys.argv[1], sys.argv[2:]
    return {"link": cmd_link, "check": cmd_check, "score": cmd_score}[cmd](files)


if __name__ == "__main__":
    sys.exit(main())
