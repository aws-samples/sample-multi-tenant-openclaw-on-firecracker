#!/bin/bash
# 启动 OpenClaw Management Console
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8080}"

# 从 .env.deploy 生成 config.js
ENV_FILE="$SCRIPT_DIR/.env.deploy"
CONFIG_JS="$SCRIPT_DIR/console/config.js"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
  cat > "$CONFIG_JS" << EOF
window.OC_DEFAULT_API_URL = "${API_URL:-}";
window.OC_DEFAULT_API_KEY = "${API_KEY:-}";
EOF
  echo "✓ 已从 .env.deploy 加载配置"
else
  echo '// no .env.deploy found' > "$CONFIG_JS"
  echo "⚠ 未找到 .env.deploy，需手动输入 API URL 和 Key"
fi

echo "→ OpenClaw Console: http://localhost:${PORT}"
cd "$SCRIPT_DIR/console"
python3 -m http.server "$PORT"
