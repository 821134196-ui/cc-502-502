#!/usr/bin/env bash
# 一条本地命令：初始化 SQLite + 混批返箱样例 + 启动服务（已初始化则保留数据）
# 想重建演示数据：./run.sh --reset
set -e
cd "$(dirname "$0")"
PORT="8000"
RESET=""
for arg in "$@"; do
  case "$arg" in
    --reset) RESET="--reset" ;;
    *) PORT="$arg" ;;
  esac
done
exec python3 -m toteflow $RESET --port "$PORT"
