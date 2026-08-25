#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#458 —— host 开机从 S3 拉的每个键都必须有推送方,且间接前缀不许漏。

## 这道门挡的是什么

`init-host.sh` 【本体】由 CDK BucketDeployment 分发(deploy/stacks/ha_edge.py 的
destination_key_prefix),asset hash 一变就自动重传。而它开机时【拉】的东西 —— 七个前缀下的
几十个对象 —— 全部由 `setup.sh` 手动 `aws s3 cp` 推。

于是「改了代码 + cdk deploy」会更新 init-host.sh 本体,却【不会】更新它要拉的东西:
新机启动时用【新】init-host.sh 去拉【旧】资产,而 host 侧一直没有本地兜底 → 静默起坏。

这不是假想。两次生产实例:
  · #265:改了代码忘重跑 setup.sh → S3 stale → host 静默起坏(setup.sh:402-404 的注释自己
    写明了这个失效模式);
  · #458:#353 patch kit 把 fluent-bit 配置只 scp 到存量机 /etc/fluent-bit/,没 promote 到
    开机真正读的 deployment/observability/fluent-bit/host/ → 8/7 新起的客户 host 拉回 353
    之前的旧配置,51 个租户 guest 日志全断;而老机之所以正常,只因为它们恰好没重启过 ——
    重启一次就退化(init-host 每次开机 aws s3 cp --recursive 会把旧版覆盖回 /etc/fluent-bit/)。

## 为什么必须机械化,而不是写进文档

claw-patch-skill 的 layer-playbook.md「Layer B — userdata pulled from S3 at boot」给的
唯一检测手段是:
    grep 's3 cp.*deployment/scripts' setup.sh
这条 grep 【命中不了】 deployment/observability/,所以照 playbook 办事的人会系统性漏掉
fluent-bit / adot 那一批。#353 漏、#428 把它写成 "deploy-other → in place" +
"Do NOT run setup.sh" 而不给替代路径,都是这一条的下游。文档补一句话拦不住下一次。

## 判据

从 init-host.sh 抽出【每一个】S3 拉取键(含通过 installer 的第二跳间接前缀),逐个要求:
  ① 该键在 setup.sh 里有对应的推送(裸 aws s3 cp 或 _obs_upload),或由 CDK BucketDeployment
     的 destination_key_prefix 覆盖;
  ② 它所属的前缀被 layer-playbook.md 的 Layer B 检测法涵盖 —— 即 playbook 里明文列出了
     这个前缀,而不是只提 deployment/scripts。

任一条不满足即 exit 1。加第三个前缀、或哪个 kit 又漏 promote,这道门直接打红。

用法:python3 scripts/checks/s3-at-boot-parity.py [--repo-root .]
退出:0=全部对上;1=有键没推送方或有前缀不在 playbook 里。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# init-host.sh 里 `s3://${ASSETS_BUCKET}/<key>` 的键部分。允许 shell 变量与 {{占位符}}。
_S3_PULL = re.compile(r"s3://\$\{ASSETS_BUCKET\}/([A-Za-z0-9_./{}$-]+)")

# 【第二跳】init-host 只拉 installer 本体,配置由 installer 自己按 role 拼前缀再 --recursive
# key: installer 路径, value: (前缀模板, 角色取值列表)
_INDIRECT = {
    "deploy/edge/fluent-bit/install-fluent-bit.sh": (
        "deployment/observability/fluent-bit/{role}",
        # host 由 init-host.sh 以 FB_ROLE=host 调用;edge 由 edge 侧同一脚本以 FB_ROLE=edge 调用。
        ["host", "edge"],
    ),
}

# 不由 setup.sh 推、而由别的机制供给的前缀,逐个写明理由(不是豁免清单,是归属说明)。
_OTHER_PUBLISHERS = {
    "{{ROOTFS_PREFIX}}": "build-rootfs.sh 产出 + 上传(rootfs/data/immutable/manifest.json)",
    "skills": "setup.sh 的 skills 同步段 + host 侧 cron aws s3 sync(见 init-host.sh step3c)",
}


def _strip_comments(text: str) -> str:
    """剥掉整行注释再匹配 —— 解释这件事的注释本身会让断言命中它自己。

    本仓被这个坑咬过多次(见 memory strip-comments-before-window-assertions):#435 有两次
    假绿就是注释里出现了被搜的字符串。
    """
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def _keys_pulled_at_boot(root: Path) -> set[str]:
    src = _strip_comments((root / "deploy/userdata/init-host.sh").read_text(encoding="utf-8"))
    keys = set(_S3_PULL.findall(src))
    for installer, (tmpl, roles) in _INDIRECT.items():
        itext = (root / installer).read_text(encoding="utf-8")
        # 前缀模板必须真的还在 installer 里 —— 否则本表已漂,宁可报错不许静默放行
        if "deployment/observability/fluent-bit/${FB_ROLE}" not in itext:
            raise SystemExit(
                f"FATAL: {installer} 里找不到 "
                "`deployment/observability/fluent-bit/${FB_ROLE}` —— "
                "本门的 _INDIRECT 表已漂,更新它而不是删掉这条检查"
            )
        for role in roles:
            keys.add(tmpl.format(role=role) + "/fluent-bit.conf")
    return keys


def _prefix_of(key: str) -> str:
    return key.rsplit("/", 1)[0] if "/" in key else key


def _setup_push_targets(root: Path) -> set[str]:
    """setup.sh 里所有推到 assets bucket 的目标键(裸 s3 cp 与 _obs_upload 两种形态)。"""
    src = _strip_comments((root / "setup.sh").read_text(encoding="utf-8"))
    targets: set[str] = set()
    # 形态 1:aws s3 cp <src> "s3://${BUCKET}/<key>"
    targets |= set(re.findall(r's3://\$\{BUCKET\}/([A-Za-z0-9_./${}-]+)', src))
    # 形态 2:_obs_upload "<src>" "<key>"
    targets |= set(re.findall(r'_obs_upload\s+"[^"]+"\s+"([A-Za-z0-9_./-]+)"', src))
    return targets


def _sync_prefixes(root: Path) -> set[str]:
    """`aws s3 sync <dir>/ s3://<bucket>/<prefix>/` —— 【整目录递归】推送方。

    这是第三个推送方,而且是最容易被漏判的一个:engineering/deploy/clawpool-deploy.sh:177
    把整个 deploy/userdata/ sync 到 deployment/scripts/,所以它连 lib/ 与 spire-kit/ 这些
    子目录一起覆盖。只按"键级精确命中 setup.sh"判定会把它们误报成"没有推送方"。

    与 setup.sh 的裸 s3 cp 是【互补而非重复】的两条路:setup.sh 是首次部署/手工重跑,
    clawpool-deploy.sh 是标准部署通道。两条都存在,恰恰说明这个分发面靠人记得跑,
    这正是 #265/#458 的根因。
    """
    out: set[str] = set()
    for rel in ("engineering/deploy/clawpool-deploy.sh", "setup.sh"):
        p = root / rel
        if not p.exists():
            continue
        text = _strip_comments(p.read_text(encoding="utf-8"))
        for m in re.finditer(
            r'aws s3 sync\s+\S+\s+"?s3://\$?\{?[A-Za-z_]+\}?/([A-Za-z0-9_./-]+)', text
        ):
            out.add(m.group(1).rstrip("/"))
    return out


def _cdk_prefixes(root: Path) -> set[str]:
    """CDK BucketDeployment 的 destination_key_prefix —— 自动分发的那一批。"""
    out: set[str] = set()
    for p in (root / "deploy/stacks").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        #   destination_key_prefix=f"{OBS_PREFIX}/{sub_prefix}" if sub_prefix else OBS_PREFIX
        # 于是那个新推送方对本门**完全不可见**,4 个开机必需对象被报成「没有推送方」——
        # 本门拒掉的恰好是它自己要求的那个修复。取整行里出现的所有标识符(含 f-string 的
        # {VAR}),再去模块级找它们的字面量:覆盖判定本来就是前缀级的,拿到
        # `deployment/observability` 就足以覆盖它下面的子前缀。
        for m in re.finditer(r"destination_key_prefix\s*=\s*([^\n]+)", text):
            expr = m.group(1)
            for var in set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)):
                for vm in re.finditer(
                    rf'^{re.escape(var)}\s*=\s*(?:f?")([A-Za-z0-9_./{{}}-]+)',
                    text,
                    re.MULTILINE,
                ):
                    out.add(vm.group(1).rstrip("/"))
    return out


# 只在【确证是公开树】时才允许 playbook 判据退场的内部标记。两者都是 EXCLUDE_DIRS:内部树里
# 必然存在,发布产物里必然不存在,所以"两个都没有"是公开树的正判据,而不是"文件恰好读不到"。
_INTERNAL_MARKERS = (".claude", "engineering")


def _playbook_prefixes(root: Path) -> str | None:
    """layer-playbook.md 的原文;确证是公开树时返回 None 让该判据退场。

    为什么要退场:这个文件在 `.claude/` 下,发布产物里永远没有它,所以本门跟着
    scripts/checks/ 发布出去之后会在公开树上直接 FileNotFoundError 崩掉 —— 崩溃被外层读成
    "发现开机分发面有缺口",把一个环境问题报成产品缺陷。它约束的是【内部照 playbook 打
    patch 的人会不会漏前缀】,公开侧没有 playbook 也就没有这个失败模式。
    **只让这一条退场**:推送方覆盖那一条(①)在公开侧同样成立且更重要,绝不能一起放过。

    为什么不能只判 `exists()`:那样在内部把 playbook 删掉或改名,也会走进同一条退场分支,
    这道判据就静默失效而 CI 照样报绿 —— 正是本门存在要挡的那类假绿。所以只有在**内部标记
    也一并缺席**时才认定公开树;内部标记在而 playbook 不在 = 本表已漂,按同文件 `_INDIRECT`
    漂移检查的同款做法直接拒绝,不许静默放行。
    """
    f = root / ".claude/skills/claw-patch-skill/references/layer-playbook.md"
    if f.exists():
        return f.read_text(encoding="utf-8")
    present = [m for m in _INTERNAL_MARKERS if (root / m).exists()]
    if present:
        raise SystemExit(
            f"FATAL: 找不到 {f.relative_to(root)},但内部标记 {present} 仍在 —— "
            "这是内部树而 playbook 被删/改名了,本判据会静默失效。"
            "更新路径而不是让它退场"
        )
    print(
        "  · 确证为发布产物树(内部标记 "
        f"{list(_INTERNAL_MARKERS)} 均不存在)——「前缀是否写进 playbook」这条判据"
        "本次不适用,已跳过;推送方覆盖仍照常判"
    )
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args(argv)
    root = Path(args.repo_root).resolve()

    keys = _keys_pulled_at_boot(root)
    pushed = _setup_push_targets(root)
    cdk = _cdk_prefixes(root)
    synced = _sync_prefixes(root)
    playbook = _playbook_prefixes(root)

    # 判别力自证:抽不到键就说明正则/路径失配,这道门会在空集合上恒真 = 等于没有门。
    if len(keys) < 10:
        print(
            f"FATAL: 只从 init-host.sh 抽到 {len(keys)} 个 S3 键(期望 >=10)——"
            "正则或路径失配,门在空集合上恒真,拒绝放行",
            file=sys.stderr,
        )
        return 1

    unpublished: list[str] = []
    undocumented: set[str] = set()

    for key in sorted(keys):
        prefix = _prefix_of(key)
        # 归属说明里的前缀:由别的机制供给
        if any(prefix.startswith(k) or key.startswith(k) for k in _OTHER_PUBLISHERS):
            continue
        # ① 有推送方?三条路任一命中即算:
        #    键级精确命中 setup.sh / CDK BucketDeployment 前缀覆盖 / s3 sync 整目录覆盖。
        exact = key in pushed
        by_cdk = any(prefix == c or prefix.startswith(c + "/") for c in cdk)
        by_sync = any(prefix == s or prefix.startswith(s + "/") for s in synced)
        # 含 shell 变量的键(如 deployment/scripts/${_s}.sh)按前缀级判定:同前缀下有推送即算
        var_key = "$" in key or "{{" in key
        by_prefix = any(_prefix_of(t) == prefix for t in pushed)
        if not (exact or by_cdk or by_sync or (var_key and by_prefix)):
            unpublished.append(key)
        # ② 前缀在 layer-playbook 的 Layer B 检测法里明文出现?
        if playbook is not None and prefix not in playbook:
            undocumented.add(prefix)

    ok = True
    if unpublished:
        ok = False
        print(
            "S3_AT_BOOT_UNPUBLISHED: init-host.sh 开机要拉这些键,但 setup.sh / CDK 里"
            "找不到推送方 —— 新机会拉到旧版或拉不到(host 无本地兜底 → 静默起坏,#265/#458):",
            file=sys.stderr,
        )
        for k in unpublished:
            print(f"  - {k}", file=sys.stderr)
    if undocumented:
        ok = False
        print(
            "S3_AT_BOOT_UNDOCUMENTED: 这些前缀是开机分发面,但 claw-patch-skill 的 "
            "layer-playbook.md 里没有明文列出 —— 照 playbook 打 patch 的人会系统性漏掉它们"
            "(#353 漏 fluent-bit、#428 写成 in-place 而不给 S3 promote,都是这一条的下游):",
            file=sys.stderr,
        )
        for p in sorted(undocumented):
            print(f"  - {p}", file=sys.stderr)

    if ok:
        # playbook 缺席时不能照抄那半句,否则这行会宣称一条本次根本没跑的判据。
        documented = (
            f"{len({_prefix_of(k) for k in keys})} 个前缀全部在 layer-playbook 里明文列出"
            if playbook is not None
            else "playbook 判据本次不适用(未跳过推送方判据)"
        )
        print(
            f"✓ s3-at-boot-parity(#458):{len(keys)} 个开机拉取键全部有推送方,{documented}"
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
