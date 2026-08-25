#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# run-hooks.sh — 在【构建实例上】按字典序执行客户 hook 目录里的脚本。
#
# 客户要加第二个步骤只能改那一个文件或改 HCL —— 前者把互不相关的步骤揉进一份脚本,
# 后者正是 D5 要禁的"改主流程"。目录 + 字典序让每个步骤是独立文件,加删互不影响。
#
# 执行时机由 host-golden.pkr.hcl 夹死在 provision 之后、两条 validate 断言之前。
# 这个位置不是任选的,三个边界各自封死一侧:
#   - provision 之后:组件(firecracker/jailer/awscli/ADOT/Fluent Bit)已装好,hook 才有
#     东西可依赖;
#   - scrub 之后(scrub 在 provision-host.sh §7 内部):所以 hook 写入的任何 per-host
#     状态【不会再被清理】,会原样进镜像并被整个机队共享 —— 这正是下一条断言存在的理由;
#   - 断言之前:断言是 hook 的唯一防线。放到断言之后,hook 留下的主机密钥、
#     platform.env、cloud-init 实例态就没人检出,fail-closed 属性会静默失效。
# assert-parity.sh 按行号校验这一顺序,`packer-pipeline-gate.py` 复核它没被挪动。
#
# 用法: run-hooks.sh <hooks-dir>
# 退出码: 0=全部 hook 成功(或目录为空), 非 0=某个 hook 失败(立即中止,不跑后续)
#
# 副产物: <hooks-dir>/../hooks-manifest —— 逐条记录实际执行顺序与每个 hook 的 sha256。
# 它留在镜像里,所以起一台 host 就能核对"这批镜像装了哪些客户内容、内容是什么版本"
# (#537 US-2 与 US-6 的可见结果)。构建侧的 HooksSha tag 由 build-golden-ami.sh 独立
# 算一遍并与本文件对账 —— 两侧独立计算才能发现"上传的和执行的不是同一批"。

set -euo pipefail

HOOKS_DIR="${1:?usage: run-hooks.sh <hooks-dir>}"
MANIFEST="$(dirname "$HOOKS_DIR")/hooks-manifest"

# 字典序枚举。用 find + sort 而不是 shell glob:glob 无匹配时会把模式串本身当文件名
# 传下去(nullglob 默认关),那会让空目录变成"执行一个叫 *.sh 的文件"并报 not found。
# -maxdepth 1:hook 是平铺的一层,不递归 —— 递归会让"子目录里的辅助脚本"被当成 hook
# 各自执行一次,而客户很可能把数据文件也放进去。
mapfile -t hooks < <(find "$HOOKS_DIR" -maxdepth 1 -type f -name '*.sh' -print 2>/dev/null | sort)

{
  echo "# hooks-manifest — 本镜像实际执行过的客户 hook,按执行顺序"
  echo "# 由 deploy/packer/run-hooks.sh 在构建实例上生成。行格式: <sha256>  <basename>"
  echo "count=${#hooks[@]}"
} > "$MANIFEST"

if [ "${#hooks[@]}" -eq 0 ]; then
  # 空目录是合法状态(客户没有自定义需求),不是错误。显式打印:构建日志里必须能看出
  # "确实一个 hook 都没有",否则"我的 hook 没被执行"与"我的 hook 目录放错地方了"
  # 这两种情况在日志上长得一样。
  echo "[oc:hooks] no hooks in $HOOKS_DIR — skipping the customization stage"
  chmod 0644 "$MANIFEST"
  exit 0
fi

echo "[oc:hooks] ${#hooks[@]} hook(s) to run, in this order:"
for h in "${hooks[@]}"; do
  printf '[oc:hooks]   %s  %s\n' "$(sha256sum "$h" | cut -d' ' -f1)" "$(basename "$h")"
done

for h in "${hooks[@]}"; do
  _name="$(basename "$h")"
  _sha="$(sha256sum "$h" | cut -d' ' -f1)"
  echo "[oc:hooks] ── running $_name"
  # 显式 bash 而不是靠 shebang + 执行位:客户从 Windows/S3 拷来的脚本常常丢执行位或
  # 带 CRLF 行尾。执行位在 V-1 前置门里已经查过并要求修好;这里用 bash 调是第二层
  # 保险,让"位丢了"表现为门拒绝而不是构建中途 Permission denied。
  #
  # 不吞任何退出码:set -e 会在 hook 失败时立刻中止整个构建,后续 hook 不再执行。
  # 这是刻意的 —— hook 之间可能有依赖(50 装包、60 配它),让 60 在 50 失败后仍跑
  # 只会产出一个半成品镜像,而那个镜像会通过后面的断言(断言查的是我们的组件,
  # 不是客户的)。fail-fast 才能让"镜像烤成了"等价于"每个 hook 都成功了"。
  bash "$h"
  printf '%s  %s\n' "$_sha" "$_name" >> "$MANIFEST"
  echo "[oc:hooks] ── done $_name"
done

chmod 0644 "$MANIFEST"
echo "[oc:hooks] all ${#hooks[@]} hook(s) completed; manifest -> $MANIFEST"
