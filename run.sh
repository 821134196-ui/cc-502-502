#!/usr/bin/env bash
# 一条命令：初始化 SQLite（含混批返箱样例）并启动本地服务。
# 用法: ./run.sh [--reset] [端口]
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
RESET=""
for arg in "$@"; do
  case "$arg" in
    --reset) RESET="--reset" ;;
    *) [[ "$arg" =~ ^[0-9]+$ ]] && PORT="$arg" ;;
  esac
done

python3 -m app.seed $RESET
echo "------------------------------------------------------------"
python3 -m app.server --port "$PORT"
