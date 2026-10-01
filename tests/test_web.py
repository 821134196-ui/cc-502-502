"""HTTP 层测试：登录会话、角色越权 403、返箱页面与折抵结算联动。"""
from __future__ import annotations

import http.cookiejar
import tempfile
import unittest
import urllib.request
from pathlib import Path

from toteflow import config, services
from toteflow.db import connect, init_db
from toteflow.web import App

from http.server import ThreadingHTTPServer

WH = {"name": "仓库-王", "role": "warehouse"}
FIN = {"name": "财务-李", "role": "finance"}


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        config.DB_PATH = d / "web.db"
        config.PHOTO_DIR = d / "photos"
        config.ensure_dirs()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), App().handler())
        cls.port = cls.srv.server_address[1]
        import threading
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

        conn = connect(config.DB_PATH)
        init_db(conn)
        services.add_customer(conn, "C001", "甲客户")
        services.add_batch(conn, "B1", "批次一")
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def client(self, user, pwd):
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op_noredir = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj), NoRedirect)

        data = f"user={user}&pwd={pwd}".encode()
        try:
            op_noredir.open(f"http://127.0.0.1:{self.port}/login", data=data)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
        return op, op_noredir

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_login_required_and_bad_password(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        nr = urllib.request.build_opener(NoRedirect)
        # 未登录访问受保护页 → 302 到 /login
        try:
            nr.open(self.url("/accounts"))
            self.fail("应重定向")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            self.assertIn("/login", e.headers["Location"])
        # 错误密码 → 401
        try:
            nr.open(self.url("/login"), b"user=wh&pwd=wrong")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_pages_render_for_warehouse(self):
        op, _ = self.client("wh", "wh123")
        for path in ("/accounts", "/returns", "/repair", "/overdue",
                     "/ledger", "/dispatch"):
            with op.open(self.url(path)) as r:
                self.assertEqual(r.status, 200)
                self.assertIn("周转箱", r.read().decode())

    def test_role_finance_cannot_dispatch_or_accept(self):
        _, nr = self.client("fin", "fin123")

        def post(path, form):
            req = urllib.request.Request(
                self.url(path), data=form.encode(), method="POST")
            try:
                nr.open(req)
                return 200
            except urllib.error.HTTPError as e:
                return e.code

        code = post("/dispatch",
                    "box_no=T-A-9001&box_type=标准箱-A&batch_id=B1&cid=C001")
        self.assertEqual(code, 403, "财务发箱必须被拒绝")

        # 先让仓库发一箱
        conn = connect(config.DB_PATH)
        services.dispatch(conn, box_no="T-A-9002", box_type="标准箱-A",
                          batch_id="B1", cid="C001", actor=WH)
        conn.close()
        code = post("/returns/accept",
                    "box_no=T-A-9002&cid=C001&body_ok=1&lid_ok=1&label_ok=1")
        self.assertEqual(code, 403, "财务不能做返箱验收")

    def test_role_warehouse_cannot_review_ledger(self):
        conn = connect(config.DB_PATH)
        services.dispatch(conn, box_no="T-A-9003", box_type="标准箱-A",
                          batch_id="B1", cid="C001", actor=WH)
        services.accept_return(conn, box_no="T-A-9003", cid="C001",
                               return_batch_id=None,
                               body_ok=True, lid_ok=True, label_ok=True,
                               actor=WH)
        lid = conn.execute(
            "SELECT id FROM deposit_ledger WHERE box_no='T-A-9003' "
            "AND status='pending'").fetchone()["id"]
        conn.close()

        _, nr = self.client("wh", "wh123")
        req = urllib.request.Request(
            self.url("/ledger/review"),
            data=f"id={lid}&approve=1".encode(), method="POST")
        try:
            nr.open(req)
            self.fail("仓库复核应 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)
        # 流水仍是待审
        conn = connect(config.DB_PATH)
        st = conn.execute("SELECT status FROM deposit_ledger WHERE id=?",
                          (lid,)).fetchone()["status"]
        conn.close()
        self.assertEqual(st, "pending")

    def test_owner_mismatch_via_preview_api(self):
        conn = connect(config.DB_PATH)
        services.add_customer(conn, "C002", "乙客户")
        services.dispatch(conn, box_no="T-A-9004", box_type="标准箱-A",
                          batch_id="B1", cid="C001", actor=WH)
        conn.close()

        op, _ = self.client("wh", "wh123")
        req = urllib.request.Request(
            self.url("/returns/preview"),
            data="box_no=T-A-9004&cid=C002".encode(), method="POST")
        try:
            op.open(req)
            self.fail("归属错误应 4xx")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 422)
            import json
            self.assertIn("不属于", json.loads(e.read())["error"])

    def test_full_return_flow_then_finance_review(self):
        conn = connect(config.DB_PATH)
        services.dispatch(conn, box_no="T-A-9005", box_type="标准箱-A",
                          batch_id="B1", cid="C001", actor=WH)
        conn.close()

        op, _ = self.client("wh", "wh123")
        # 仓库验收合格
        body = urllib.parse.urlencode({
            "box_no": "T-A-9005", "cid": "C001",
            "body_ok": "1", "lid_ok": "1", "label_ok": "1"}).encode()
        with op.open(self.url("/returns/accept"), data=body) as r:
            page = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn("验收结论", page)
        self.assertIn("客户净退押金", page)  # 折抵金额紧挨验收结论

        check = connect(config.DB_PATH)
        row = check.execute(
            "SELECT id FROM deposit_ledger WHERE box_no='T-A-9005' "
            "AND tx_type='REFUND'").fetchone()
        ledger_id = row["id"]
        check.close()

        # 财务复核通过
        fop, fnr = self.client("fin", "fin123")
        req = urllib.request.Request(
            self.url("/ledger/review"),
            data=f"id={ledger_id}&approve=1".encode(), method="POST")
        try:
            fnr.open(req)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
        c = connect(config.DB_PATH)
        rec = c.execute("SELECT status,receipt_no FROM deposit_ledger WHERE id=?",
                        (ledger_id,)).fetchone()
        c.close()
        self.assertEqual(rec["status"], "confirmed")
        self.assertTrue(rec["receipt_no"].startswith("MOCK-RFD-"))


if __name__ == "__main__":
    unittest.main()
