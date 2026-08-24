#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#523 判据 2 — host 启动两条路径的内核/Firecracker pin 必须逐字相同。

病根:同一个 pin 写了两份。`provision-host.sh` 的那份决定**烤进 AMI 的**
Firecracker 二进制与 guest kernel;`init-host.sh` 的那份决定**开机时**取哪个版本
(golden 路径优先用镜像里的副本,plain 路径按自己的名字去下载)。两份从来没有任何
一致性校验,所以只改一处(或改了两处但没重烤 AMI)的后果不是报错,而是**机队混着两个
内核 / 两个 Firecracker 在跑**,而 `create` / `rebuild` / `restart` 全部经
`launch-vm.sh` 用 `${ASSETS}/vmlinux` —— 同一租户在不同 host 上被重建,拿到的内核
可能不同,且没有任何断言会发现。

这道门与 `init-host.sh` 的开机 parity 断言是**两个不同时刻**,不互相替代:
  · 本门(merge 期):抓"只改了一处 pin"。改坏的代码进不了 bb。
  · 开机断言(boot 期):抓"两处都改了但没重烤 AMI"。那种情况代码是自洽的,
    只有真机上 marker 与常量不符,唯有开机时能发现。

做法照 `#476` 的 `FORBIDDEN_BY_PIN`(`scripts/checks/template-schema-gate.py`):
不追求"收敛成一处",因为 provision 必须能在 bake 时**独立运行**(那时 init-host 根本
不参与)且**不许携带 `{{}}` 占位符**(`deploy/stacks/ha_edge.py` 的 synth 期护栏 —— 它被
烤进全机队共享的 AMI),而 init-host 在 golden 机上必须能在 provision **从不运行**的
前提下自己解析出内核名。两者各留一份常量、由一道 fail-loud 的门锁住逐字相等,是这
个约束下的正确形态。

用法:
    host-pin-parity.py            # 扫 deploy/userdata/ 下那两个文件
退出:0=两份逐字相同;1=不一致,或任一处的 pin 抽不出来(拒过,不静默放行)。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: 两条启动路径各自的 pin 载体。左=bake 时用,右=boot 时用。
BAKE_PATH = "deploy/userdata/provision-host.sh"
BOOT_PATH = "deploy/userdata/init-host.sh"

#: 受管 pin。每项 = (人类可读名, 正则)。正则必须在**每个**载体里命中【恰好一次】,
#: 且捕获组按顺序就是要比对的值。命中 0 次或 >1 次都算失败:
#:   0 次 → 有人改了写法(改名、拆多行、换成读文件),比对无从进行,必须 fail-loud
#:          而不是"扫不到就算过"——那会让这道门在重构后静默失效。
#:   >1 次 → 同一文件里出现第二份 pin,本门比的是"跨文件相同",管不住"同文件自相矛盾"。
#: 加新 pin 时:在这里加一行,两个载体的写法保持逐字一致的形状。
MANAGED_PINS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "FC_VER (Firecracker release tag, ${FC_VERSION} 的默认值)",
        re.compile(r'^\s*FC_VER="\$\{FC_VERSION:-([^}"]+)\}"\s*$', re.M),
    ),
    (
        "VMLINUX_NAME (guest kernel object, aarch64 / x86_64 两个分支)",
        re.compile(
            r'^\s*if \[ "\$\{ARCH\}" = "aarch64" \]; then VMLINUX_NAME="([^"]+)"; '
            r'else VMLINUX_NAME="([^"]+)"; fi\s*$',
            re.M,
        ),
    ),
)


def _extract(text: str, pattern: re.Pattern[str], where: str, name: str) -> tuple[str, ...]:
    """抽出 pin 的值。命中数不为 1 就抛,绝不返回"抽不到"这种可被当成通过的结果。"""
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f"{where}: `{name}` 命中 {len(matches)} 次(要求恰好 1 次)。"
            "写法变了就得同步改 MANAGED_PINS 的正则 —— 扫不到不等于没问题。"
        )
    only = matches[0]
    return (only,) if isinstance(only, str) else tuple(only)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    bake = repo_root / BAKE_PATH
    boot = repo_root / BOOT_PATH
    for path in (bake, boot):
        if not path.is_file():
            print(f"❌ host-pin-parity: 找不到 {path}", file=sys.stderr)
            return 1

    bake_text = bake.read_text(encoding="utf-8")
    boot_text = boot.read_text(encoding="utf-8")

    problems: list[str] = []
    checked = 0
    for name, pattern in MANAGED_PINS:
        try:
            bake_vals = _extract(bake_text, pattern, BAKE_PATH, name)
            boot_vals = _extract(boot_text, pattern, BOOT_PATH, name)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        checked += 1
        if bake_vals != boot_vals:
            problems.append(
                f"{name} 两条启动路径不一致:\n"
                f"      {BAKE_PATH}(烤进 AMI)= {list(bake_vals)}\n"
                f"      {BOOT_PATH}(开机取件)= {list(boot_vals)}"
            )

    if problems:
        print("❌ host-pin-parity(#523 判据 2):内核/Firecracker pin 两份不一致,拒过:", file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)
        print(
            "  修:两处改成同一个值。改 pin 后还必须重烤 golden AMI —— 只改代码不重烤,"
            "在役 golden 机队仍跑旧版本,那一档由 init-host.sh 的开机 parity 断言拦。",
            file=sys.stderr,
        )
        return 1
    print(f"✓ host-pin-parity(#523):{checked} 项 pin 在 bake / boot 两条路径上逐字相同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
