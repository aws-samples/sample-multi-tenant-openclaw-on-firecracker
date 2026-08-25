#!/usr/bin/env bash
# 断言每一处 DynamoDB Scan 都翻页。DDB 单次 Scan 上限 1MB 且 FilterExpression 在那 1MB 读
# 【之后】才过滤 —— 决定页数的是全表字节数,不是命中数。openclaw-tenants 实测 6790 行 /
# 2.83MB(已超 1MB 近三倍),openclaw-hosts 39 行里 33 行是 deleted 死行且只增不减。
# 不翻页的后果全是静默看错:选点看不见后页 host → 有容量却报 unplaced;TTL 扫不到后页租户
# → 永不过期且持续计费;健康检查扫不到 → 坏了没人发现;疏散扫不到 → 租户留在死机器上。
# 本仓早把这条纪律写在 _registered_host_count 的注释里,42 处里仍有 17 处没照做 → 机械化。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$DIR/lib.sh"

ck_hdr "ddb-scan-pagination · 每处 DynamoDB Scan 都必须翻页(#432)"

if ! have python3; then
  ck_warn "无 python3,跳过 ddb-scan-pagination(CI 侧 python 环境会兜底)"
  exit 0
fi

if python3 "$DIR/ddb-scan-pagination.py" --repo-root "$DIR/../.." >/tmp/ck-ddbscan.log 2>&1; then
  ck_ok "$(tail -1 /tmp/ck-ddbscan.log)"
  exit 0
else
  ck_bad "ddb-scan-pagination 发现裸 Scan(拒过):"
  sed 's/^/      /' /tmp/ck-ddbscan.log | head -28 >&2
  exit 1
fi
