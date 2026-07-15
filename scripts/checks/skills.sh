#!/usr/bin/env bash
# skills.sh — golden-image skill 安全扫描(第一层机械门,红队 CI 门 · issue #85)。
#
# 为什么:本仓 samples/*/skills/ 会烤进 microVM 只读盘,一条恶意 skill 里如果混进
# eval/exec/os.system/shell=True/prompt injection/凭据文件读取,租户 agent 就直接暴露。
# vet 这道门以前只在 samples/finance-agent/skills/skill-vetter/ 里有,是**给用户
# vet 三方 skill 用的**——但没在 CI 上跑,自家 samples 变更没有机械门兜底。issue #85
# 把它接进 run-all.sh 的全量分支,让 CI 检查门(跑 run-all.sh)天然继承阻断。
#
# 阈值(--fail-on critical 是默认):
#   - CRITICAL 命中(eval/exec/os.system/shell=True/凭据/身份文件写/prompt injection)→ exit 1
#   - HIGH/MEDIUM 只提示不挡门(正常 skill 用 requests/open() 会命中 MEDIUM,挡这些
#     会把所有真 skill 都拦下)。要更严跑 CK_SKILLS_FAIL_ON=any 全挡。
#
# 用法:scripts/checks/skills.sh
#       CK_SKILLS_FAIL_ON=any scripts/checks/skills.sh   # 严格模式(挡到 INFO 之上)
#       CK_SKILLS_TARGET=<path> scripts/checks/skills.sh # 只扫指定目录(测试夹具用)
# 退出:0=全过;1=有 skill 命中(默认)CRITICAL。
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "skills · samples/*/skills/*/ 恶意模式扫描(skill-vetter scan.py)"
rc=0

# skill-vetter 的 scan.py 本身
SCANNER="$CK_ROOT/samples/finance-agent/skills/skill-vetter/scripts/scan.py"
if [ ! -f "$SCANNER" ]; then
  ck_warn "skill-vetter scan.py 不存在($SCANNER),跳过扫描"
  ck_dim "如需启用,恢复 samples/finance-agent/skills/skill-vetter/scripts/scan.py"
  exit 0
fi

# 找 python3(macOS 有 python3、CI 也装了)
if ! have python3; then
  ck_warn "python3 未装,跳过 skill 扫描(命根子门是 secrets 已过)"
  exit 0
fi

fail_on="${CK_SKILLS_FAIL_ON:-critical}"
target_root="${CK_SKILLS_TARGET:-$CK_ROOT}"

# 枚举 skill 目录:samples/*/skills/ 下每个直接子目录就是一个 skill
# (_clis/ 是共享 CLI 目录,内含独立子包但不是 skill,单独判断跳过)
# 用换行分隔字符串 + IFS 兼容 bash 3.2(macOS 默认没 mapfile / bash 4+)
skill_dirs="$(find "$target_root/samples" -mindepth 3 -maxdepth 3 -type d -path '*/skills/*' 2>/dev/null | sort)"

if [ -z "$skill_dirs" ]; then
  ck_ok "无 samples/*/skills/*/ 目录"
  exit 0
fi

total_dirs="$(printf '%s\n' "$skill_dirs" | wc -l | tr -d ' ')"
ck_dim "扫描 $total_dirs 个 skill 目录,阈值 --fail-on $fail_on"

hits=0
scanned=0
skipped=0

while IFS= read -r skill_dir; do
  [ -n "$skill_dir" ] || continue
  skill_name="$(basename "$skill_dir")"
  # $skill_dir = samples/<sample>/skills/<skill_name>
  # sample_name = basename of grandparent
  sample_name="$(basename "$(dirname "$(dirname "$skill_dir")")")"
  display="samples/$sample_name/skills/$skill_name"

  # skill-vetter 自扫必命中(patterns.md 里全是攻击模式样例作为文档,scan.py 会以为
  # 自己在提供攻击手册):它是 vetter 本身的实现,不参与被 vet,跳过。
  if [ "$skill_name" = "skill-vetter" ]; then
    ck_dim "  跳过 $display(vet 工具自身,patterns.md 是模式样例)"
    skipped=$((skipped+1))
    continue
  fi

  # _clis 是共享 CLI 目录不是 skill,跳过。
  if [ "$skill_name" = "_clis" ]; then
    ck_dim "  跳过 $display(共享 CLI 目录)"
    skipped=$((skipped+1))
    continue
  fi

  scanned=$((scanned+1))
  # 用 --fail-on 阈值让 scan.py 自己决定 exit code
  if python3 "$SCANNER" "$skill_dir" --fail-on "$fail_on" >/tmp/ck-skills.log 2>&1; then
    ck_ok "$display"
  else
    ck_bad "$display(命中 $fail_on 及以上,详见 /tmp/ck-skills.log):"
    # 只截取 CRITICAL/HIGH 前 30 行,别把 MEDIUM 噪音全 dump 出来
    grep -E 'CRITICAL|HIGH' /tmp/ck-skills.log | head -30 | sed 's/^/      /' >&2 || true
    hits=$((hits+1))
    rc=1
  fi
done <<EOF
$skill_dirs
EOF

echo
if [ "$hits" -eq 0 ]; then
  ck_ok "skill 恶意模式扫描:$scanned 个 skill 全过(跳过 $skipped 个:skill-vetter/_clis)"
else
  ck_warn "$hits 个 skill 命中,修掉再进 MR;确属误报调 scan.py 的 pattern 或调 CK_SKILLS_FAIL_ON"
fi
exit $rc
