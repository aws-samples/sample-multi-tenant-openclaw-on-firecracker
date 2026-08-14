#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# build-guide-html.sh — 把 CUSTOMER-GUIDE.md 渲染成单文件 HTML,用于交付给客户。
#
# 为什么要单文件:交付物经邮件/S3/内网盘流转,外链 CSS 与 JS 在客户环境常被拦或失效。
# 样式全部内联,无外部请求,离线双击即可阅读。
#
# 为什么不入库产物:HTML 是 md 的派生物,两份都入库必然出现"改了 md 忘了重新生成"的
# 漂移。产物写到 dist/ 并 gitignore,交付时现场生成。
#
# 用法: deploy/packer/build-guide-html.sh [输出路径]
# 退出码: 0=成功, 2=前置缺失

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/CUSTOMER-GUIDE.md"
OUT="${1:-$HERE/dist/CUSTOMER-GUIDE.html}"

command -v pandoc >/dev/null 2>&1 || {
  echo "FATAL: 未找到 pandoc。安装: brew install pandoc" >&2; exit 2; }
[ -f "$SRC" ] || { echo "FATAL: 找不到 $SRC" >&2; exit 2; }

mkdir -p "$(dirname "$OUT")"

# 样式内联进 header-includes。--standalone 产出完整 HTML 文档,--toc 生成目录
# (手册有 8 节,没有目录客户要靠滚动找)。关掉 pandoc 的语法高亮:
# 它注入的 class 需要配套 CSS,自带一份反而与下面的样式冲突;代码块用等宽字体
# 加浅背景已足够,少一层依赖。
cat > "$HERE/.guide-style.html" <<'STYLE'
<style>
  :root {
    --fg: #1f2328; --muted: #59636e; --line: #d1d9e0; --bg: #ffffff;
    --code-bg: #f6f8fa; --accent: #0969da; --warn-bg: #fff8c5; --warn-line: #d4a72c;
  }
  html { -webkit-text-size-adjust: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    line-height: 1.7; color: var(--fg); background: var(--bg);
    max-width: 900px; margin: 0 auto; padding: 2.5rem 1.5rem 6rem;
  }
  h1 { font-size: 1.9rem; border-bottom: 2px solid var(--line); padding-bottom: .5rem; margin-bottom: .3rem; }
  h2 { font-size: 1.4rem; border-bottom: 1px solid var(--line); padding-bottom: .35rem; margin-top: 2.8rem; }
  h3 { font-size: 1.12rem; margin-top: 2rem; color: #24292f; }
  p, li { font-size: .95rem; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  code {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: .86em; background: var(--code-bg); padding: .15em .4em;
    border-radius: 4px; border: 1px solid var(--line);
  }
  pre {
    background: var(--code-bg); border: 1px solid var(--line); border-radius: 6px;
    padding: .9rem 1rem; overflow-x: auto; line-height: 1.5;
  }
  pre code { background: none; border: none; padding: 0; font-size: .84rem; }
  table { border-collapse: collapse; width: 100%; margin: 1.2rem 0; font-size: .9rem; }
  th, td { border: 1px solid var(--line); padding: .5rem .7rem; text-align: left; vertical-align: top; }
  th { background: var(--code-bg); font-weight: 600; }
  tr:nth-child(even) td { background: #fbfcfd; }
  blockquote { border-left: 4px solid var(--line); margin: 1rem 0; padding: .2rem 1rem; color: var(--muted); }
  hr { border: none; border-top: 1px solid var(--line); margin: 2.5rem 0; }
  /* pandoc --toc 的容器 */
  nav#TOC {
    background: var(--code-bg); border: 1px solid var(--line); border-radius: 6px;
    padding: 1rem 1.4rem; margin: 1.8rem 0 2.5rem;
  }
  nav#TOC > ul { margin: 0; padding-left: 1.1rem; }
  nav#TOC ul ul { font-size: .9em; }
  nav#TOC li { margin: .18rem 0; }
  nav#TOC::before {
    content: "目录"; display: block; font-weight: 600; font-size: .95rem;
    margin-bottom: .5rem; color: var(--fg);
  }
  /* 打印:客户常把手册打出来照着做 */
  @media print {
    body { max-width: none; padding: 0; font-size: 10.5pt; }
    nav#TOC { break-after: page; }
    h2 { break-before: auto; break-after: avoid; }
    pre, table { break-inside: avoid; }
    a { color: var(--fg); }
  }
</style>
STYLE

pandoc "$SRC" \
  --standalone \
  --toc --toc-depth=3 \
  --syntax-highlighting=none \
  --metadata title="使用 Packer 构建 host golden AMI — 操作手册" \
  --metadata lang=zh \
  --include-in-header="$HERE/.guide-style.html" \
  -o "$OUT" 2> >(grep -vE "Could not load translations|data file translations|has no translation defined" >&2)

# 过滤掉的两条 WARNING 是 pandoc 自带数据的限制,不是本脚本的配置问题:
# pandoc 未附带 translations/zh.yaml,而它唯一会用到的中文词条是 "Abstract"
# (仅当文档带 abstract 元数据时才渲染),本手册没有 abstract。留着这两条警告
# 会让客户以为生成有问题。其余 stderr 原样透出。
rm -f "$HERE/.guide-style.html"

# 两处 pandoc 默认行为需要修正,都会影响客户第一眼看到的内容:
#   1. --metadata title 生成一个 title 块,与 md 里的一级标题重复出现两个大标题;
#   2. --toc 把目录插在 <body> 开头,于是目录排在文档标题【之前】—— 客户打开先看到
#      一堆链接才看到这是什么文档。把 TOC 移到 h1 与首段之后。
python3 - "$OUT" <<'PYEOF'
import re, sys

path = sys.argv[1]
html = open(path, encoding="utf-8").read()

# 1) 去掉 pandoc 的 title 块,保留 md 里的 h1
html = re.sub(r'<header id="title-block-header">.*?</header>\n?', '', html, flags=re.S)

# 2) 把 TOC 从 body 开头移到第一个 <hr /> 之后(md 里 h1 与导语之后正是一条 ---)。
#    找不到锚点时保持原位而不是猜 —— 顺序不理想胜过把目录插进正文中间。
m_toc = re.search(r'<nav id="TOC".*?</nav>\n?', html, flags=re.S)
if m_toc:
    toc = m_toc.group(0)
    body = html[:m_toc.start()] + html[m_toc.end():]
    m_hr = re.search(r'<hr\s*/?>\n?', body)
    if m_hr:
        html = body[:m_hr.end()] + toc + body[m_hr.end():]

open(path, "w", encoding="utf-8").write(html)
PYEOF

echo "已生成: $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes)"
echo "自检:"
grep -c '<h2' "$OUT" | sed 's/^/  h2 章节数: /'
grep -c '<table' "$OUT" | sed 's/^/  表格数: /'
grep -c '<pre' "$OUT" | sed 's/^/  代码块数: /'
# 外部引用是这份交付物唯一的功能性风险:有外链就不是"离线可读"。
if grep -qE '<(link|script)[^>]+(src|href)="https?:' "$OUT"; then
  echo "  ⚠ 检出外部资源引用 —— 该文件不是自包含的" >&2
  exit 1
fi
echo "  外部资源引用: 0(自包含)"
