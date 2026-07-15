#!/usr/bin/env bash
# 停 LiteLLM 栈。默认保留 postgres 卷（spend/vkey 不丢）。
# 传 --wipe 才连卷一起删（销毁计费数据，慎用）。
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--wipe" ]; then
  echo "[down] 停栈并删除卷（postgres spend/vkey 将丢失）..."
  docker compose -f docker-compose.litellm.yml down -v
else
  echo "[down] 停栈，保留卷（spend/vkey 持久化）..."
  docker compose -f docker-compose.litellm.yml down
fi
echo "[down] 完成。"
