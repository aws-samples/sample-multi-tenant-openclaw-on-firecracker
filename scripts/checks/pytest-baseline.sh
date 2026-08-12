#!/usr/bin/env bash
# pytest-baseline.sh — 并行跑全量 pytest,并与「无本地改动」的 baseline 逐条对比,
# 只报【本次改动新增】的失败。
#
# 为什么需要它:本仓 tests/ 有 4600+ 用例、约 150 条【既有失败】(pull_image 44 /
# oc_issue 20 / entry_topology 9 …,与任何单次改动无关)。串行跑一轮 14 分钟,
# 且总失败数从来不是 0,所以"跑一遍看红不红"无法判断自己有没有引入回归 ——
# 必须拿 baseline 做差集。
#
# 加速:pytest-xdist -n(默认 10,留 2 核给系统)+ --dist loadfile(同文件同 worker,
# 避免模块级 fixture 跨 worker 重复初始化)。实测 14:00 → 7:55(省 43%),
# 且并行与串行的失败集【逐条一致】(2026-08-10 核验),故并行安全。
#
# 用法:
#   scripts/checks/pytest-baseline.sh                 # 对比模式(默认):跑 base ref 的代码作 baseline,再跑 after,报差集
#   scripts/checks/pytest-baseline.sh --after-only    # 只跑当前工作树(已有 baseline 缓存时省一半时间)
#   OC_PYTEST_JOBS=6 scripts/checks/pytest-baseline.sh  # 自定义并行度
#   OC_PYTEST_BASE=<ref> scripts/checks/pytest-baseline.sh  # 自定义对比基线(默认 origin/bb)
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || exit 2

PY="${OC_PYTEST_PY:-$ROOT/.venv/bin/python}"
JOBS="${OC_PYTEST_JOBS:-10}"
OUT_DIR="${TMPDIR:-/tmp}/oc-pytest-baseline"
BASE_TXT="$OUT_DIR/base.txt"
AFT_TXT="$OUT_DIR/after.txt"
mkdir -p "$OUT_DIR"

[ -x "$PY" ] || { echo "GATE_FAIL: 找不到解释器 $PY(设 OC_PYTEST_PY)" >&2; exit 2; }
"$PY" -c "import xdist" 2>/dev/null || {
  echo "GATE_FAIL: 缺 pytest-xdist。装:uv pip install pytest-xdist" >&2; exit 2; }

# 这些文件在【本仓当前状态】就无法收集(sys.path/模块级 import 问题),与改动无关。
# 收集错误会让 pytest 整轮 Interrupted,故必须排除,否则拿不到任何对比数据。
IGNORES=( --ignore=tests/test_187_get_tenant_fold_ciphertext.py )

# 对比模式下要一并排除,才能让两轮口径相同。
NEW_TESTS=(
  tests/test_430_capacity_host_profile.py
  tests/test_430_binpack_affinity.py
  tests/test_430_find_host_affinity.py
  tests/test_430_asg_mixed_pool_synth.py
  tests/test_430_batch_mem_gate_and_register.py
)

# ★ 这些文件在 synth 前【就地重写 config.yml】(读→改→写→finally 还原)。并行下
# --dist loadfile 会把它们分到不同 worker,多个 worker 同时改同一个 config.yml →
# 互相踩,轻则测试假失败,重则 finally 写回的是别人的中间态、把 404 行注释全抹掉
# (2026-08-10 实撞:并行跑这 6 个文件后 config.yml 注释归零)。
# 故:它们【串行】跑,其余文件才并行。
#
# ★ test_rbac.py 不在此列(虽然它也重写 config.yml):它用 importlib 直接 exec
# handler.py,而 handler.py 的 `from core.logging import ...` 需要
# deploy/lambda/api 在 sys.path 上 —— 它自己不插。串行段是【显式点名】跑它,
# 于是 sys.path 上没有 api、collect 必炸,整个串行段 Interrupted、一条结果都拿不到
# (--ignore 对显式点名无效,所以只能不点它)。并行段里它能否收集取决于同 worker
# 有没有别的文件先插过 sys.path(如 test_dispatch_service.py:26),随 --dist loadfile
# 分配漂移。stash 掉任何改动后单跑同样失败 → 既有脆弱耦合,与本次改动无关。
# 代价:它落回并行段,与其他 config 重写者同跑。已实测(2026-08-11,-n 5 --dist loadfile
# 跑全部 5 个 config 重写者 + test_rbac):config.yml 689 行逐字节未变 —— 因为它在并行段
# 是 collect 就失败(sys.path 缺 api),根本没跑到重写 config 的代码。
# 它在并行段的 collect ERROR 不会中断整轮(xdist 下单文件失败只废那一个 worker 的该文件),
# 所以差集仍拿得到,两轮口径一致。根治要让它自己插 sys.path,属独立 issue。
CONFIG_REWRITERS=(
  tests/test_graviton.py
  tests/test_multi_az.py
  tests/test_prometheus.py
  tests/test_dual_domain.py
  tests/test_430_asg_mixed_pool_synth.py
)

_run() {  # $1=工作目录  $2=日志路径  $3..=额外 pytest 参数
  local wd="$1" log="$2"; shift 2
  local _rc_par _rc_ser
  # cd 进目标目录:baseline 轮跑临时 worktree(base 代码),after 轮跑当前工作树。
  # conftest 与测试都靠相对路径找 deploy/,所以必须在对应目录里跑,不能只换 -p。
  cd "$wd" || { echo "GATE_FAIL: 进不去 $wd" >&2; return 2; }
  # 两段:① 并行跑绝大多数(排除 config 重写者) ② 串行跑 config 重写者,追加进同一日志
  "$PY" -m pytest tests/ -q -p no:cacheprovider -n "$JOBS" --dist loadfile \
    "${IGNORES[@]}" "${CONFIG_REWRITERS[@]/#/--ignore=}" "$@" > "$log" 2>&1
  _rc_par=$?
  "$PY" -m pytest "${CONFIG_REWRITERS[@]}" -q -p no:cacheprovider \
    "$@" >> "$log" 2>&1
  _rc_ser=$?
  # 只有 pytest 的 0(全过)/1(有测试失败)算"正常完成"。2=中断 3=内部错 4=用法错
  # 5=没收集到用例 —— 这些情况下日志里可能【一条 FAILED/ERROR 都没有】,差集就会把
  # "整轮没跑起来"读成"零新增失败"(2026-08-11 实撞:test_rbac collect 失败让串行段
  # 整段 Interrupted,5 个文件一条结果都没有,差集照样报绿)。故此处 fail-loud。
  local _rc
  for _rc in "$_rc_par" "$_rc_ser"; do
    case "$_rc" in
      0|1) ;;
      *) echo "GATE_FAIL: pytest 异常退出码 $_rc(2=中断/3=内部错/4=用法错/5=零用例)" >&2
         echo "  日志尾部:" >&2; tail -5 "$log" | sed 's/^/    /' >&2
         return 2 ;;
    esac
  done
  tail -1 "$log" | sed 's/^/  /'
}

_extract() {  # 从日志抽出唯一失败条目(去掉 " - <原因>" 后缀,原因含路径/内存地址会抖动)
  rg "^(FAILED|ERROR) " "$1" | sed -E 's/ - .*//' | sort -u
}

# 校验一轮日志真的【跑完了】而不是半途而废。_extract 只看 FAILED/ERROR 行,
# 收集中断/进程被杀时那些行可能一条都没有 → 空失败集被读成"干净"。
# 判据:必须出现 pytest 的结尾 summary 行(含 passed/failed 计数),且不能出现
# Interrupted。两段各一次,故至少 2 条 summary。
_assert_complete() {  # $1=日志 $2=轮次名
  local log="$1" name="$2" n_sum n_int
  n_sum=$(rg -c '^[0-9]+ (passed|failed)|[0-9]+ (passed|failed)(,|\s|$)' "$log" 2>/dev/null || echo 0)
  n_int=$(rg -c 'Interrupted|INTERNALERROR' "$log" 2>/dev/null || echo 0)
  if [ "${n_int:-0}" -gt 0 ]; then
    echo "GATE_FAIL: $name 轮日志含 Interrupted/INTERNALERROR($n_int 处)—— 该轮未完整执行," >&2
    echo "  失败集不可用于差集判定。先修收集错误(或加进 IGNORES)再重跑。" >&2
    rg -n 'Interrupted|INTERNALERROR|^ERROR ' "$log" | head -5 | sed 's/^/    /' >&2
    return 2
  fi
  if [ "${n_sum:-0}" -lt 2 ]; then
    echo "GATE_FAIL: $name 轮只找到 ${n_sum:-0} 条 pytest summary(应 >=2:并行段+串行段)" >&2
    echo "  说明有一段没跑完,失败集不完整。" >&2
    return 2
  fi
}

if [ "${1:-}" = "--after-only" ]; then
  [ -s "$BASE_TXT" ] || { echo "GATE_FAIL: 无 baseline 缓存($BASE_TXT),先跑一次对比模式" >&2; exit 2; }
  echo "== after-only(复用 baseline 缓存 $(wc -l < "$BASE_TXT") 条)=="
else
  # baseline = 【base ref 的代码】,在【临时 worktree】里跑。
  #
  # 演进史(两次都踩过,都写在这里免得再犯):
  # ① 最早用 `git stash push -- deploy/ config.yml.example`:只藏得住【未提交】的改动。
  #    改动一旦 commit(正常开发流程边做边提交),两轮跑的就是同一份代码、差集恒为空 ——
  #    报出的"零新增"是假绿(2026-08-11 实撞:连报几轮零新增,换对比方式后立刻暴露
  #    6 条真回归)。
  # ② 接着改成 `git checkout <base> -- deploy/` + 跑完 `git checkout HEAD -- ...` 还原:
  #    差集口径对了,但会【不可逆覆盖】这些路径下已有的 staged/unstaged 改动 ——
  #    未提交的工作直接消失,且中途 git add 会把 base 版暂存成"回退"(2026-08-11 实撞)。
  # ③ 现在:临时 worktree。工作树和 index 一行不碰,base 代码在别处 checkout,
  #    跑完 `worktree remove --force` 清掉。唯一代价是多一次 checkout 的磁盘/时间。
  #
  # OC_PYTEST_BASE 可覆盖 base ref(默认 origin/bb)。
  BASE_REF="${OC_PYTEST_BASE:-origin/bb}"
  git rev-parse --verify --quiet "$BASE_REF^{commit}" >/dev/null || {
    echo "GATE_FAIL: base ref '$BASE_REF' 不存在(设 OC_PYTEST_BASE)" >&2; exit 2; }
  BASE_WT="$OUT_DIR/wt-baseline"
  echo "== 1/2 baseline:在临时 worktree 跑 $BASE_REF 的代码 =="
  # 残留清理:上次被 kill 可能留下 worktree 记录
  git worktree remove --force "$BASE_WT" >/dev/null 2>&1 || true
  rm -rf "$BASE_WT"
  # shellcheck disable=SC2064  # 立即展开 BASE_WT
  trap "git worktree remove --force '$BASE_WT' >/dev/null 2>&1 || true; rm -rf '$BASE_WT'" EXIT INT TERM
  git worktree add -q --detach "$BASE_WT" "$BASE_REF" || {
    echo "GATE_FAIL: 建 worktree 失败($BASE_WT)" >&2; exit 2; }
  # tests/ 用【当前】版本:差集要归因到 deploy/ 代码差异,测试文件两轮必须同一份
  # (否则新增的测试文件在 baseline 轮 collect 失败,把噪声记进差集)。
  rm -rf "$BASE_WT/tests" && cp -R tests "$BASE_WT/tests"
  # 子 shell 跑:_run 内部 cd,不能污染主 shell 的 cwd(after 轮要在主工作树跑)。
  # venv 不进 worktree(gitignored),$PY 是绝对路径故可直接用。
  ( _run "$BASE_WT" "$OUT_DIR/baseline.log" "${NEW_TESTS[@]/#/--ignore=}" ) || exit 2
  _assert_complete "$OUT_DIR/baseline.log" baseline || exit 2
  _extract "$OUT_DIR/baseline.log" > "$BASE_TXT"
  git worktree remove --force "$BASE_WT" >/dev/null 2>&1 || true
  rm -rf "$BASE_WT"
  trap - EXIT INT TERM
  echo "  baseline($BASE_REF)既有失败:$(wc -l < "$BASE_TXT") 条"
fi

echo "== 2/2 after:跑当前工作树 =="
( _run "$ROOT" "$OUT_DIR/after.log" "${NEW_TESTS[@]/#/--ignore=}" ) || exit 2
_assert_complete "$OUT_DIR/after.log" after || exit 2
_extract "$OUT_DIR/after.log" > "$AFT_TXT"
echo "  当前失败:$(wc -l < "$AFT_TXT") 条"

echo
echo "== 差集判定 =="
NEW_FAILS="$(comm -13 "$BASE_TXT" "$AFT_TXT")"
FIXED="$(comm -23 "$BASE_TXT" "$AFT_TXT")"
# shellcheck disable=SC2086  # 故意不加引号:按空白拆成多行,每条失败一行
[ -n "$FIXED" ] && { echo "  顺带修好(baseline 有、现在没了):"; printf '    %s\n' $FIXED; }

if [ -n "$NEW_FAILS" ]; then
  echo "  ⛔ 本次改动【新增】失败:"
  # shellcheck disable=SC2086  # 同上,故意拆词
  printf '    %s\n' $NEW_FAILS
  echo
  echo "日志:$OUT_DIR/{baseline,after}.log"
  exit 1
fi
echo "  ✓ 零新增失败(既有失败 $(wc -l < "$BASE_TXT") 条与本次改动无关)"
echo
echo "== 新增测试单独跑(baseline 里不存在,无法进差集) =="
"$PY" -m pytest "${NEW_TESTS[@]}" -q -p no:cacheprovider 2>&1 | tail -3
