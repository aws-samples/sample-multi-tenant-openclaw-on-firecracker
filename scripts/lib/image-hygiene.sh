#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# image-hygiene.sh — #35 镜像卫生 · 剥 Python 构建缓存 + fail-loud 校验。
#
# 抽出来是为了两件事:
#   ① build-rootfs.sh 的 immutable/data 两条路径复用同一份 strip+assert 逻辑,
#      避免谓词漂移(此前 immutable 断言的 *.pyc 缺 -type f,与 strip 侧不一致)。
#   ② 让 tests/test_image_hygiene.py 可以直接 subprocess 跑真 bash,验证
#      "谓词、返回码、fail-loud 行为"的实际字节层,而不是 Python 复刻近似断言。
#
# 三个函数,POSIX-friendly(bash source 用):
#   image_hygiene_strip <root>        剥掉 __pycache__/.ruff_cache/.pytest_cache/.mypy_cache
#                                      目录 + 所有 *.pyc 文件。等价于 build 侧原 strip。
#   image_hygiene_find_hits <root>    输出仍在的缓存目录/*.pyc 路径清单(fail-loud 用)。
#   image_hygiene_assert <root> [tag] 找到任何缓存 → stderr 打出命中项 + 返回 1,
#                                      调用方按需再 umount/exit。tag 是打印时的路径遮罩前缀。
#
# 无 sudo 依赖:调用方自行按需 sudo,这里只做 find 语义。测试从临时目录 tmp 起,
# 无需 sudo;build-rootfs.sh 在挂载点上跑,自己带 sudo 调用。

set -eu

# ---- helpers ----

# 归一化 find 谓词:目录 -type d,*.pyc -type f。两处保持一致,避免"漏加 -type f
# 把 pycache 名的目录/文件搞混"的谓词漂移(评审 LOW #3)。
_image_hygiene_cache_dir_predicate() {
  # 用法:_image_hygiene_cache_dir_predicate <root>
  # 打印匹配的目录路径(空白分隔安全的 -print0 版由调用方按需换)。
  find "$1" \
    \( -name '__pycache__' -o -name '.ruff_cache' -o -name '.pytest_cache' -o -name '.mypy_cache' \) \
    -type d -prune -print 2>/dev/null || true
}

_image_hygiene_pyc_predicate() {
  # 用法:_image_hygiene_pyc_predicate <root>
  find "$1" -name '*.pyc' -type f -print 2>/dev/null || true
}

# ---- public API ----

# 剥掉 root 下所有 __pycache__/.ruff_cache/.pytest_cache/.mypy_cache 目录 +
# 全部 *.pyc 文件。symlink 一律不跟随(find 默认不跟随 -type d/-type f),
# 避免误删符号链接指向的外部目录(评审 MEDIUM #2b)。
image_hygiene_strip() {
  root="${1:?image_hygiene_strip: need <root>}"
  # -prune 后 -exec rm -rf 目录本体;-type d/-type f 与 find_hits 谓词严格一致。
  find "${root}" \
    \( -name '__pycache__' -o -name '.ruff_cache' -o -name '.pytest_cache' -o -name '.mypy_cache' \) \
    -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "${root}" -name '*.pyc' -type f -delete 2>/dev/null || true
}

# 列出所有仍留在 root 下的缓存命中(缺省逐行 print)。返回 0(找不到 = 空)。
# 调用方判空:`[ -n "$(image_hygiene_find_hits …)" ]`。
image_hygiene_find_hits() {
  root="${1:?image_hygiene_find_hits: need <root>}"
  {
    _image_hygiene_cache_dir_predicate "${root}"
    _image_hygiene_pyc_predicate "${root}"
  }
}

# fail-loud 断言:root 下有任何缓存残留 → stderr 打出清单 + return 1。
# 用法:image_hygiene_assert <mounted-root> [tag-for-mask]
#   mounted-root 是真实的挂载点(build-rootfs 里的 IMMUTABLE_DIR / DATA_DIR),
#   tag 用来把绝对路径遮罩成 <immutable> / <data> 之类,避免泄漏真实路径。
image_hygiene_assert() {
  root="${1:?image_hygiene_assert: need <root>}"
  tag="${2:-<image>}"
  hits="$(image_hygiene_find_hits "${root}")"
  if [ -n "${hits}" ]; then
    printf '✗ #35 image hygiene FAILED — build cache leaked into %s:\n' "${tag}" >&2
    printf '%s\n' "${hits}" | sed "s|${root}|${tag}|g" >&2
    return 1
  fi
  return 0
}

# 允许作为独立脚本调用,便于 CI 和临时排查:
#   scripts/lib/image-hygiene.sh strip <root>
#   scripts/lib/image-hygiene.sh assert <root> [tag]
#   scripts/lib/image-hygiene.sh hits <root>
# 只在被直接执行时进入分派,source 时不动。
_image_hygiene_main() {
  cmd="${1:-}"
  # shift 在无参数时 + set -e 下会炸,先判再动。
  if [ "$#" -gt 0 ]; then shift; fi
  case "${cmd}" in
    strip) image_hygiene_strip "$@" ;;
    hits) image_hygiene_find_hits "$@" ;;
    assert) image_hygiene_assert "$@" ;;
    *)
      cat >&2 <<USAGE
image-hygiene.sh — #35 镜像卫生工具库
用法(独立执行):
  $0 strip <root>          # 剥 __pycache__/.ruff_cache/.pytest_cache/.mypy_cache/*.pyc
  $0 hits <root>           # 列出残留(空 = 干净)
  $0 assert <root> [tag]   # 命中即 exit 1,配合 build 的 fail-loud 校验
或 source 后调用同名函数。
USAGE
      exit 2
      ;;
  esac
}

# 被"直接执行"时进 main;被 source 时跳过——BASH_SOURCE[0] 是当前文件路径,
# $0 是入口脚本名/交互 shell 名,只有被直接执行时两者相等。
# 兼容非 bash(无 BASH_SOURCE):当作被 source,不进 main,保守不动。
if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
  _image_hygiene_main "$@"
fi
