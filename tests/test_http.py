"""HTTP 端到端测试：X-Role 鉴权、发箱/返箱/折抵、照片上传与角色越权。"""
from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import server
from app.db import connect, init_db
from app import services as svc

PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f0b0000000049454e44ae426082"
)).decode()


def req(method, path, body=None, role="admin"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        f"http://127.0.0.1:{server_thread_port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "X-Role": role})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


server_thread_port = 0


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        server.DATA = d
        server.DB_PATH = d / "t.db"
        server.PHOTOS_DIR = d / "photos"
        server.RECEIPTS_DIR = d / "receipts"
        server.PHOTOS_DIR.mkdir(parents=True)
        conn = connect(server.DB_PATH); init_db(conn)
        svc.create_box_type(conn, "R60", "60L", 6000, 3000, 9000)
        cls.c1 = svc.create_customer(conn, "C01", "客户甲")
        cls.c2 = svc.create_customer(conn, "C02", "客户乙")
        conn.close()

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        global server_thread_port
        server_thread_port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def test_01_issue_as_warehouse(self):
        s, r = req("POST", "/api/issue",
                   {"customer_id": self.c1, "type_code": "R60", "qty": 2,
                    "due_at": "2026-10-20", "batch_code": "HB1"}, "warehouse")
        self.assertEqual(s, 201)
        self.assertEqual(len(r["boxes"]), 2)
        self.assertEqual(r["deposit_total_cents"], 12000)
        self.__class__.codes = [x["box_code"] for x in r["boxes"]]

    def test_02_finance_forbidden_issue(self):
        s, r = req("POST", "/api/issue",
                   {"customer_id": self.c1, "type_code": "R60", "qty": 1,
                    "due_at": "2026-10-20"}, "finance")
        self.assertEqual(s, 403)
        self.assertEqual(r["error"], "FORBIDDEN")

    def test_03_return_ok_and_wrong_owner(self):
        ok = self.codes[0]
        # 给客户乙再发一箱，作为“非本客户箱号”
        s, _ = req("POST", "/api/issue",
                   {"customer_id": self.c2, "type_code": "R60", "qty": 1,
                    "due_at": "2026-10-25", "batch_code": "HB2"}, "warehouse")
        s, boxes = req("GET", f"/api/customers/{self.c2}/boxes")
        alien = boxes[0]["code"]

        s, r = req("POST", "/api/return",
                   {"customer_id": self.c1,
                    "items": [{"box_code": ok, "verdict": "OK"},
                              {"box_code": alien, "verdict": "OK"}],
                    "expected_batch_id": None}, "warehouse")
        self.assertEqual(s, 200)
        self.assertEqual(r["totals"]["refund_cents"], 6000)
        reasons = [x["reason"] for x in r["rejected"]]
        self.assertIn("NOT_OWNED", reasons)

    def test_04_duplicate_concurrent_return(self):
        s, r = req("POST", "/api/issue",
                   {"customer_id": self.c2, "type_code": "R60", "qty": 1,
                    "due_at": "2026-10-25", "batch_code": "HB3"}, "warehouse")
        code = r["boxes"][0]["box_code"]
        import concurrent.futures
        def call():
            return req("POST", "/api/return",
                       {"customer_id": self.c2,
                        "items": [{"box_code": code, "verdict": "OK"}]},
                       "warehouse")
        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            (s1, r1), (s2, r2) = list(ex.map(lambda _: call(), [0, 1]))
        acc = len(r1.get("accepted", [])) + len(r2.get("accepted", []))
        self.assertEqual(acc, 1)

    def test_05_repair_with_photo_then_review_forbidden_for_warehouse(self):
        s, r = req("POST", "/api/issue",
                   {"customer_id": self.c1, "type_code": "R60", "qty": 1,
                    "due_at": "2026-10-20", "batch_code": "HB4"}, "warehouse")
        code = r["boxes"][0]["box_code"]
        s, r = req("POST", "/api/return",
                   {"customer_id": self.c1,
                    "items": [{"box_code": code, "verdict": "REPAIR",
                               "body_ok": False,
                               "photo_data": f"data:image/png;base64,{PNG_1PX}",
                               "note": "裂纹"}]}, "warehouse")
        self.assertEqual(s, 200)
        line = r["accepted"][0]
        self.assertTrue(line["photo_path"].endswith(".png"))
        self.assertEqual(line["deduct_cents"], 3000)
        self.assertTrue(line["provisional"])

        # 照片可取回
        with urllib.request.urlopen(
                f"http://127.0.0.1:{server_thread_port}/{line['photo_path']}",
                timeout=5) as p:
            self.assertEqual(p.status, 200)
            self.assertEqual(p.read()[:4], b"\x89PNG")

        # 仓库不能复核折抵 —— box id 通过维修池接口获取
        s, pool = req("GET", "/api/repair-pool")
        bid = [x for x in pool if x["code"] == code][0]["id"]
        s, r = req("POST", "/api/finance/review",
                   {"box_id": bid, "action": "CONFIRM"}, "warehouse")
        self.assertEqual(s, 403)

        # 财务确认
        s, r = req("POST", "/api/finance/review",
                   {"box_id": bid, "action": "CONFIRM",
                    "repair_fee_yuan": 20, "note": "按工单20元"}, "finance")
        self.assertEqual(s, 200)
        s, led = req("GET", f"/api/ledger?customer_id={self.c1}", role="finance")
        confirmed = [x for x in led if x["box_code"] == code
                     and x["kind"] == "REPAIR_DEDUCT"][0]
        self.assertEqual(confirmed["amount_cents"], 2000)
        self.assertEqual(confirmed["provisional"], 0)

    def test_06_return_context_shows_three_counts(self):
        s, r = req("GET", f"/api/return-context?customer_id={self.c1}")
        self.assertIn("should_return", r)
        self.assertIn("returned_ok", r)
        self.assertIn("in_repair", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
