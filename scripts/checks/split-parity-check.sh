#!/usr/bin/env bash
# split-parity-check.sh — 拆分前后「不重不漏不错」逐字校验(视觉校验的程序化版)。
#
# 证明 god file 拆分后:① 不漏(拆前每个函数拆后都在) ② 不重(无重复 def,facade 别名不算)
# ③ 不错(搬走的函数体逐字符相同,0 差异)。比测试更硬:测试证行为不变,这个证代码一字没改。
# 用法:split-parity-check.sh <拆前基线commit> <目标包目录>
# 例:split-parity-check.sh fcfa7d3 deploy/lambda/api
set -u
BASE="${1:?用法: $0 <拆前commit> <包目录>}"; PKG="${2:?}"
HANDLER="$PKG/handler.py"
git show "$BASE:$HANDLER" > /tmp/_split_before.py 2>/dev/null || { echo "取不到 $BASE:$HANDLER"; exit 1; }
python3 - "$PKG" <<'PY'
import ast,glob,sys,os
pkg=sys.argv[1]
def fb(src):
    t=ast.parse(src);L=src.splitlines();o={}
    for n in t.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)): o[n.name]="\n".join(L[n.lineno-1:n.end_lineno])
    return o
before=fb(open("/tmp/_split_before.py").read())
after={};dups=[]
for f in [f"{pkg}/handler.py"]+glob.glob(f"{pkg}/core/*.py")+glob.glob(f"{pkg}/services/*.py")+glob.glob(f"{pkg}/routes/*.py")+glob.glob(f"{pkg}/consumers/*.py"):
    if not os.path.exists(f):continue
    for name,body in fb(open(f).read()).items():
        if name in after:dups.append((name,after[name][1],f))
        after[name]=(body,f)
missing=[n for n in before if n not in after]
diff=[n for n in before if n in after and before[n].strip()!=after[n][0].strip()]
moved=sum(1 for n in before if n in after and "handler.py" not in after[n][1])
print(f"不漏: 拆前{len(before)}→拆后{len(after)}唯一, 漏={missing or '无'}")
print(f"不重: 重复def={[(d[0]) for d in dups] or '无'}")
print(f"不错: 搬走{moved}个, 函数体逐字不同={diff or '无'}")
ok = not missing and not dups and not diff
print("✓ 不重不漏不错 全过" if ok else "✗ 见上")
sys.exit(0 if ok else 1)
PY
