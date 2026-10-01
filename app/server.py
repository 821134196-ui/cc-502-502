"""零依赖 HTTP 服务：JSON API + 静态前端。

每个请求使用独立的短连接 SQLite（WAL 模式下并发安全）。
角色由请求头 X-Role 决定：warehouse / finance / admin。
破损照片以 base64 随验收 JSON 提交，落盘到 data/photos 并回显相对路径。
"""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import services as svc
from .db import connect, init_db
from .finance import MockFinanceAdapter

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT.parent / "data"
DB_PATH = DATA / "turnover.db"
PHOTOS_DIR = DATA / "photos"
RECEIPTS_DIR = DATA / "finance_receipts"

_write_lock = threading.Lock()  # 照片文件落盘互斥（DB 侧另有事务保证）

_CT = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
       "css": "text/css; charset=utf-8", "png": "image/png", "jpg": "image/jpeg",
       "jpeg": "image/jpeg", "svg": "image/svg+xml", "ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "TurnoverBox/1.0"

    # ---- 工具 ----
    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, e: svc.DomainError):
        self._send_json({"error": e.code, "message": e.message}, e.http_status)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _role(self, body: dict | None = None) -> str:
        role = self.headers.get("X-Role") or (body or {}).pop("_role", None) or "admin"
        if role not in svc.ROLES:
            raise svc.DomainError("BAD_ROLE", f"未知角色 {role}")
        return role

    def _conn(self):
        return connect(DB_PATH)

    def _finance(self, conn):
        return MockFinanceAdapter(conn, RECEIPTS_DIR)

    def log_message(self, fmt, *args):
        pass  # 静默；异常已在响应中体现

    # ---- 路由 ----
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p.startswith("/api/"):
                return self._api_get(p, q)
            return self._static(p)
        except svc.DomainError as e:
            return self._err(e)
        except FileNotFoundError:
            return self._send_json({"error": "NOT_FOUND", "message": p}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            return self._api_post(p)
        except svc.DomainError as e:
            return self._err(e)

    # ---- 静态 ----
    def _static(self, p: str):
        rel = "index.html" if p in ("", "/") else p.lstrip("/")
        # 照片目录只读访问
        if rel.startswith("photos/"):
            f = (DATA / rel).resolve()
            if not str(f).startswith(str(PHOTOS_DIR.resolve())) or not f.is_file():
                return self._send_json({"error": "NOT_FOUND"}, 404)
            data = f.read_bytes()
            ext = f.suffix.lstrip(".")
            self.send_response(200)
            self.send_header("Content-Type", _CT.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        f = (WEB / rel).resolve()
        if not str(f).startswith(str(WEB.resolve())) or not f.is_file():
            f = WEB / "index.html"
        data = f.read_bytes()
        ext = f.suffix.lstrip(".")
        self.send_response(200)
        self.send_header("Content-Type", _CT.get(ext, "text/plain; charset=utf-8"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- GET API ----
    def _api_get(self, p: str, q: dict):
        conn = self._conn()
        cid = int(q["customer_id"][0]) if q.get("customer_id") else None
        m = re.fullmatch(r"/api/customers/(\d+)/boxes", p)
        if p == "/api/meta":
            return self._send_json({"roles": [{"code": r, "name": svc.ROLE_LABELS[r]}
                                             for r in svc.ROLES]})
        if p == "/api/customers":
            return self._send_json(svc.customer_summary(conn))
        if m:
            return self._send_json(svc.customer_boxes(conn, int(m.group(1)),
                                                      q.get("state", [None])[0]))
        m = re.fullmatch(r"/api/boxes/([^/]+)/events", p)
        if m:
            return self._send_json(svc.box_events(conn, m.group(1)))
        m = re.fullmatch(r"/api/return-context", p)
        if m:
            if cid is None:
                raise svc.DomainError("NEED_CUSTOMER", "需要 customer_id")
            return self._send_json(svc.return_context(conn, cid))
        if p == "/api/types":
            return self._send_json(svc.list_box_types(conn))
        if p == "/api/batches":
            return self._send_json(svc.list_batches(conn))
        if p == "/api/repair-pool":
            return self._send_json(svc.repair_pool(conn))
        if p == "/api/overdue":
            return self._send_json(svc.overdue_list(conn))
        if p == "/api/ledger":
            return self._send_json(svc.deposit_flow(conn, cid))
        if p == "/api/disputes":
            return self._send_json(svc.list_disputes(conn))
        if p == "/api/dashboard":
            cust = svc.customer_summary(conn)
            return self._send_json({
                "customers": cust,
                "repair_pool": len(svc.repair_pool(conn)),
                "overdue": len(svc.overdue_list(conn)),
                "out_total": sum(c["out"] for c in cust),
            })
        raise svc.DomainError("NOT_FOUND", f"未知接口 {p}", 404)

    # ---- POST API ----
    def _api_post(self, p: str):
        body = self._read_json()
        role = self._role(body)
        conn = self._conn()
        fin = self._finance(conn)

        if p == "/api/issue":
            res = svc.issue_boxes(
                conn, int(body["customer_id"]), body["type_code"], int(body["qty"]),
                body["due_at"], role, body.get("batch_code"))
            return self._send_json(res, 201)

        if p == "/api/return":
            items = body.get("items", [])
            for it in items:  # 落盘 base64 破损照片
                img = it.pop("photo_data", None)
                if img:
                    it["photo_path"] = self._save_photo(it["box_code"], img)
            res = svc.return_boxes(
                conn, fin, int(body["customer_id"]), items, role,
                body.get("expected_batch_id"), bool(body.get("allow_cross_batch")),
                body.get("set_ref"))
            return self._send_json(res)

        m = re.fullmatch(r"/api/repair/(\d+)/done", p)
        if m:
            fee = svc.yuan_to_cents(body["actual_fee_yuan"]) if body.get("actual_fee_yuan") else None
            return self._send_json(svc.repair_done(conn, int(m.group(1)), role, fee))

        if p == "/api/finance/review":
            fee = svc.yuan_to_cents(body["repair_fee_yuan"]) if body.get("repair_fee_yuan") is not None else None
            comp = svc.yuan_to_cents(body["comp_yuan"]) if body.get("comp_yuan") is not None else None
            res = svc.finance_review(conn, fin, int(body["box_id"]),
                                     body["action"], role, fee, comp,
                                     body.get("note", ""))
            return self._send_json(res)

        m = re.fullmatch(r"/api/finance/settle/(\d+)", p)
        if m:
            return self._send_json(svc.settle_lost(conn, fin, int(m.group(1)), role,
                                                   body.get("note", "")))

        if p == "/api/finance/disputes":
            res = svc.open_dispute(conn, int(body["box_id"]),
                                   svc.yuan_to_cents(body["claim_yuan"]),
                                   role, body.get("note", ""))
            return self._send_json(res, 201)

        m = re.fullmatch(r"/api/finance/disputes/(\d+)/resolve", p)
        if m:
            return self._send_json(svc.resolve_dispute(
                conn, fin, int(m.group(1)), body["resolution"],
                svc.yuan_to_cents(body.get("approved_refund_yuan", 0)), role))

        raise svc.DomainError("NOT_FOUND", f"未知接口 {p}", 404)

    def _save_photo(self, box_code: str, data_url: str) -> str:
        """data:image/png;base64,.... → data/photos/<box>_<ts>_<r>.<ext>"""
        ext = "jpg"
        b64 = data_url
        mm = re.match(r"data:image/(\w+);base64,(.+)", data_url, re.S)
        if mm:
            ext, b64 = mm.group(1), mm.group(2)
            if ext == "jpeg":
                ext = "jpg"
        raw = base64.b64decode(b64)
        with _write_lock:
            name = f"{box_code}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:4]}.{ext}"
            (PHOTOS_DIR / name).write_bytes(raw)
        return f"photos/{name}"


def run(host: str = "127.0.0.1", port: int = 8000):
    DATA.mkdir(exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect(DB_PATH)
    init_db(conn)
    conn.close()
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"周转箱台账已启动 → http://{host}:{port}")
    print(f"数据库 {DB_PATH} ｜ 财务回执 {RECEIPTS_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    run(a.host, a.port)
