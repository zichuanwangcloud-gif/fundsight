#!/usr/bin/env bash
# 一键启动盈见 FundSight 后端服务。
#
# 用法:
#   ./scripts/run.sh            # 默认监听 8000 端口
#   PORT=9000 ./scripts/run.sh  # 通过环境变量覆盖端口
#
# 启动时会自动执行 backend.app.main() 内部的 init_db()（建表 + 首次写入
# 种子基金数据），随后以 http.server 监听服务，Ctrl+C 结束。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export PORT="${PORT:-8000}"

echo "启动盈见 FundSight（PORT=${PORT}）..."
exec python3 -m backend.app
