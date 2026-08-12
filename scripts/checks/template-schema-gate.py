#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#197 build 机器门 — 模板 key 集 ⊆ 目标 gateway schema,超集 fail-loud 拒烤。

病根(美东1 E2E 2026-07-12):git 内模板载体带超版本 key(6.11 才加的
heartbeat.isolatedSession/lightContext、compaction.midTurnPrecheck/
maxActiveTranscriptBytes),而镜像 pin 2026.2.26、gateway schema `.strict()`
fail-closed 拒未知 key → gateway 启动校验失败拒起 → 崩溃重启。build-rootfs.sh
原本只有一句防呆注释,靠人自觉;这里升级成真门:烤镜像前扫模板载体,发现
禁用 key 直接非零退出,CI/build 都过不去。

用法:
    template-schema-gate.py <baked-openclaw.json> [额外模板文件...]
    # 无参时扫仓内所有已知模板载体(供 CI 全量跑)

判据:pin 版本(OPENCLAW_PIN,默认从 build-rootfs.sh 读)对应的禁用 key 集合。
超版本 key 是版本相关事实——升级 OpenClaw 版本时同步更新 FORBIDDEN_BY_PIN,
不是一次性硬编码(注释写清怎么维护)。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 目标 pin 版本禁用的 config key(点分路径)。这些是 6.x 才加、2.26 `.strict()`
# schema 拒绝的键。**维护**:升级 OPENCLAW_PIN 时,对目标版 dist 的
# validateConfigObjectWithPlugins 跑一遍,把新版接受的键从此表移除、新禁的加入。
# 事实来源:opensource/openclaw 2026.2.13(与 pin 2.26 同族)src/ 全仓无这四个键
# 表里没有的 pin 一律 fail-loud 拒过(见 main() 的 forbidden is None 分支),这是有意的:
# 换 pin 必须先对目标版实测,不许先放行后补证。**不要为了让某个未实测的版本过门而给它
# 加空集条目**——空集等于「此版无禁用 key」的断言,没实测就不是断言而是猜。例:2026.6.33
# 的 schema 大概率接受下面这 4 个键(它们正是 6.x 才加的),但 6.33 从未对
# validateConfigObjectWithPlugins 实测过(且已因配对门差异回退,见 build-rootfs.sh 的
# OPENCLAW_PIN 注释),所以它不在表里,真要升 6.33 时先实测再补条目。
FORBIDDEN_BY_PIN = {
    "2026.2.26": {
        "agents.defaults.heartbeat.isolatedSession",
        "agents.defaults.heartbeat.lightContext",
        "agents.defaults.compaction.midTurnPrecheck",
        "agents.defaults.compaction.maxActiveTranscriptBytes",
        # 钩子(llm_input / llm_output),故 build-rootfs 的 plugins 注册段现在会写
        # `plugins.entries.sentinel-guard.hooks.allowConversationAccess=true`。
        # 该键在 2.26 dist 里**命中 0**(与 allowPromptInjection 同为 6.x/7.x 新增),
        # 而 2.26 的 plugins.entries.<name> schema 是 `.strict()` → 会被拒 → gateway
        # 拒起。登记在这里,任何人把 OPENCLAW_PIN 回钉 2.26 时这道门 fail-loud 拒烤,
        "plugins.entries.sentinel-guard.hooks.allowConversationAccess",
    },
    # 不是"没查所以放空"。证据(可复现命令见 evidence 文件):用 7.1-2 自己的
    # `openclaw config validate` 跑三组,**对照组会失败**所以正例才算数——
    #   ① templates/openclaw.json.example 原样            → Config valid
    #   ② 同上 + agents.defaults.compaction.__bogus_nested__ → Invalid input(对照组,证明门真在拒)
    #   ③ 同上 + 下面这 4 个 2.26-禁用键                   → Config valid
    # 源码交叉核对(7.1-2 dist,均为合法可选字段):
    #   heartbeat.isolatedSession            zod-schema.agent-runtime-C02vY4RT.js:79
    #   heartbeat.lightContext               plugin-sdk/config-schema.d.ts(ZodOptional<ZodBoolean>)
    #   compaction.midTurnPrecheck           zod-schema-O9ml_nmo.js:220
    #   compaction.maxActiveTranscriptBytes  zod-schema-O9ml_nmo.js:238
    # 边界:空集只断言"2.26 禁而 7.1-2 放行"这一向。反向(7.1-2 新禁的键)未穷举,但组 ①
    # 已证现有模板整体在 7.1-2 下合法,故当前载体无反向风险;往模板加新键时重跑组 ①。
    # evidence: engineering/evidence/openclaw-pin-7.1-2-schema-probe-2026-08-12.md
    "2026.7.1-2": set(),
}

# 仓内已知模板载体(CI 无参全量扫)。build-rootfs 传入实际烤入的那份另算。
KNOWN_CARRIERS = [
    "samples/config-templates/mcp-filesystem.json",
    "samples/config-templates/mcp-fetch-http.json",
    "samples/config-templates/mcp-git-postgres.json",
    "templates/openclaw.json.example",
    "console/js/app.templates.js",
    "deploy/console-bff/web/js/app.templates.js",
]


def _pin_version(repo_root: Path) -> str:
    """从 build-rootfs.sh 读 OPENCLAW_PIN,拿不到回退 2026.2.26。"""
    br = repo_root / "build-rootfs.sh"
    if br.is_file():
        m = re.search(r'OPENCLAW_PIN="([^"]+)"', br.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return "2026.2.26"


def _leaf_names(forbidden: set[str]) -> set[str]:
    """点分路径取叶子键名(供 JS/文本载体做子串命中,JS 不是 JSON 无法结构解析）。"""
    return {p.rsplit(".", 1)[-1] for p in forbidden}


def _scan_json(path: Path, forbidden: set[str]) -> list[str]:
    """结构化扫 JSON:命中禁用 key 的完整点分路径。"""
    hits: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"{path}: JSON 解析失败 ({e}) — 拒烤(模板必须是合法 JSON)"]

    def walk(node, prefix):
        if isinstance(node, dict):
            for k, v in node.items():
                dotted = f"{prefix}.{k}" if prefix else k
                if dotted in forbidden:
                    hits.append(f"{path}: 禁用 key `{dotted}`")
                walk(v, dotted)

    walk(data, "")
    return hits


def _scan_text(path: Path, leaf_names: set[str]) -> list[str]:
    """非 JSON 载体(JS)按叶子键名做词边界子串命中(保守:命中即报,人工核）。"""
    hits: list[str] = []
    text = path.read_text(encoding="utf-8")
    for name in leaf_names:
        if re.search(rf"\b{re.escape(name)}\b", text):
            hits.append(
                f"{path}: 禁用 key 名 `{name}`(文本载体命中,需人工核是否在 config)"
            )
    return hits


def scan(paths: list[Path], forbidden: set[str]) -> list[str]:
    leafs = _leaf_names(forbidden)
    all_hits: list[str] = []
    for p in paths:
        if not p.is_file():
            continue
        if p.suffix == ".json":
            all_hits += _scan_json(p, forbidden)
        else:
            all_hits += _scan_text(p, leafs)
    return all_hits


def main(argv: list[str]) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    pin = _pin_version(repo_root)
    forbidden = FORBIDDEN_BY_PIN.get(pin)
    if forbidden is None:
        print(
            f"⚠ template-schema-gate: pin 版本 {pin} 无禁用 key 表——"
            f"升级版本时须在 FORBIDDEN_BY_PIN 补该版本条目(fail-loud 拒过)",
            file=sys.stderr,
        )
        return 1

    args = argv[1:]
    if args:
        paths = [Path(a) for a in args]
    else:
        paths = [repo_root / c for c in KNOWN_CARRIERS]

    hits = scan(paths, forbidden)
    if hits:
        print(
            f"❌ template-schema-gate(pin {pin}): 模板含超版本 key,拒烤/拒 merge:",
            file=sys.stderr,
        )
        for h in hits:
            print(f"    - {h}", file=sys.stderr)
        print(
            "  修:从模板载体删掉这些 6.x-only key,或若确要升级 gateway 版本,"
            "改 OPENCLAW_PIN + 更新 FORBIDDEN_BY_PIN 表。",
            file=sys.stderr,
        )
        return 1
    print(f"✓ template-schema-gate(pin {pin}):{len(paths)} 个模板载体无超版本 key")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
