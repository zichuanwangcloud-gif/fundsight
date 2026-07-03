#!/usr/bin/env bash
# 运行全部单元测试（标准库 unittest，无需额外依赖）。
#
# 用法:
#   ./scripts/test.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

python3 -m unittest discover -s tests -v
