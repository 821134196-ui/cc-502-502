"""python3 -m toteflow [--reset] [--port 8000]

初始化 SQLite、播种含混批返箱的演示数据并启动本地 Web 服务。
"""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer

from . import config
from . import seed as seed_mod
from .web import App


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="周转箱押金管理（本地）")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="清空后重新播种")
    ap.add_argument("--no-seed", action="store_true", help="不播种演示数据")
    args = ap.parse_args(argv)

    config.ensure_dirs()
    if not args.no_seed:
        did = seed_mod.seed(force=args.reset)
        print(f"[toteflow] 演示数据：{'已重新播种' if args.reset else ('已播种' if did else '已存在，跳过（--reset 可重建）')}")

    handler = App().handler()
    srv = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"[toteflow] 服务已启动 → http://{args.host}:{args.port}/login")
    print("[toteflow] 仓库 wh/wh123  财务 fin/fin123  主管 admin/admin123")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[toteflow] 已停止")


if __name__ == "__main__":
    main()
