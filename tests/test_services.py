"""领域服务测试：归属、重复/并发退箱、折抵一致性、角色限制、跨批次、幂等。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.db import connect, init_db
from app.finance import MockFinanceAdapter
from app import services as svc

W = svc.ROLE_WAREHOUSE
F = svc.ROLE_FINANCE
A = svc.ROLE_ADMIN


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "t.db"
        self.rec = self.dir / "receipts"
        self.conn = connect(self.db)
        init_db(self.conn)
        self.fin = MockFinanceAdapter(self.conn, self.rec)
        svc.create_box_type(self.conn, "R60", "60L", 6000, 3000, 9000)
        svc.create_box_type(self.conn, "R80", "80L", 10000, 12000, 16000)
        self.c1 = svc.create_customer(self.conn, "C01", "客户甲")
        self.c2 = svc.create_customer(self.conn, "C02", "客户乙")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def issue(self, cid, qty, typ="R60", due="2026-10-20", code=None):
        return svc.issue_boxes(self.conn, cid, typ, qty, due, W, code)

    def box_id(self, code):
        return self.conn.execute("SELECT id FROM boxes WHERE code=?", (code,)).fetchone()["id"]

    def totals(self, cid):
        return svc._ledger_totals(self.conn, cid)


class TestOwnership(Base):
    def test_box_not_owned_by_customer(self):
        r = self.issue(self.c1, 3, code="B1")
        alien = r["boxes"][0]["box_code"]
        res = svc.return_boxes(self.conn, self.fin, self.c2,
                               [{"box_code": alien, "verdict": "OK"}], W,
                               set_ref="RC-X1")
        self.assertEqual(res["accepted"], [])
        self.assertEqual(res["rejected"][0]["reason"], "NOT_OWNED")
        # 箱子仍在客户甲处，未产生任何退款
        self.assertEqual(self.totals(self.c2)["refunded_cents"], 0)
        row = self.conn.execute("SELECT state,customer_id FROM boxes WHERE code=?",
                                (alien,)).fetchone()
        self.assertEqual(row["state"], "OUT")
        self.assertEqual(row["customer_id"], self.c1)

    def test_box_not_found(self):
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": "NOPE-9", "verdict": "OK"}], W,
                               set_ref="RC-X2")
        self.assertEqual(res["rejected"][0]["reason"], "BOX_NOT_FOUND")


class TestDuplicateReturn(Base):
    def test_duplicate_in_same_scan_set(self):
        r = self.issue(self.c1, 2, code="B2")
        code = r["boxes"][0]["box_code"]
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": code, "verdict": "OK"},
                                {"box_code": code, "verdict": "OK"}], W,
                               set_ref="RC-D1")
        self.assertEqual(len(res["accepted"]), 1)
        self.assertEqual(res["rejected"][0]["reason"], "ALREADY_RETURNED")
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM deposit_ledger l JOIN boxes b ON b.id=l.box_id "
            "WHERE b.code=? AND l.kind='REFUND'", (code,)).fetchone()["n"]
        self.assertEqual(n, 1)  # 余额只退一次

    def test_concurrent_returns_never_double_refund(self):
        """两个线程并发退同一个箱子：仅一个成功，押金只退一次。"""
        r = self.issue(self.c1, 1, code="B3")
        code = r["boxes"][0]["box_code"]
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(tag):
            conn = connect(self.db)
            fin = MockFinanceAdapter(conn, self.rec)
            barrier.wait()
            try:
                out = svc.return_boxes(
                    conn, fin, self.c1, [{"box_code": code, "verdict": "OK"}],
                    W, set_ref=f"RC-C{tag}")
                outcomes.append((len(out["accepted"]),
                                 [x["reason"] for x in out["rejected"]]))
            except Exception as e:  # 串行化冲突也算“没退成”
                outcomes.append((0, [type(e).__name__]))
            conn.close()

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start(); t2.start(); t1.join(10); t2.join(10)

        accepted = sum(a for a, _ in outcomes)
        self.assertEqual(accepted, 1, f"并发结果异常：{outcomes}")
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM deposit_ledger l JOIN boxes b ON b.id=l.box_id "
            "WHERE b.code=? AND l.kind='REFUND'", (code,)).fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(self.totals(self.c1)["refunded_cents"], 6000)

    def test_idempotent_set_ref_replay(self):
        r = self.issue(self.c1, 1, code="B4")
        code = r["boxes"][0]["box_code"]
        args = (self.conn, self.fin, self.c1,
                [{"box_code": code, "verdict": "OK"}], W)
        first = svc.return_boxes(*args, set_ref="RC-IDEM")
        second = svc.return_boxes(*args, set_ref="RC-IDEM")
        self.assertEqual(first["ref"], second["ref"])
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM deposit_ledger WHERE kind='REFUND'").fetchone()["n"]
        self.assertEqual(n, 1)


class TestCrossBatch(Base):
    def test_cross_batch_rejected_then_allowed(self):
        b1 = self.issue(self.c1, 2, code="BA")
        b2 = self.issue(self.c1, 2, code="BB")
        other = b2["boxes"][0]["box_code"]  # 属于 BB

        # 按 BA 核对、未勾选混批 → 拒收
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": other, "verdict": "OK"}], W,
                               expected_batch_id=b1["batch_id"],
                               set_ref="RC-M1")
        self.assertEqual(res["rejected"][0]["reason"], "CROSS_BATCH")

        # 勾选允许混批 → 放行且标注 cross_batch
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": other, "verdict": "OK"}], W,
                               expected_batch_id=b1["batch_id"],
                               allow_cross_batch=True, set_ref="RC-M2")
        self.assertEqual(res["accepted"][0]["cross_batch"], True)

    def test_mixed_batch_scan(self):
        b1 = self.issue(self.c1, 2, code="MC1")
        b2 = self.issue(self.c1, 1, typ="R80", code="MC2")
        res = svc.return_boxes(
            self.conn, self.fin, self.c1,
            [{"box_code": b1["boxes"][0]["box_code"], "verdict": "OK"},
             {"box_code": b2["boxes"][0]["box_code"], "verdict": "OK"}],
            W, expected_batch_id=None, set_ref="RC-MIX")
        self.assertTrue(res["mixed_batch"])
        self.assertEqual(res["totals"]["count"], 2)


class TestDeductionConsistency(Base):
    def _ledger_for_box(self, code):
        return [dict(r) for r in self.conn.execute(
            "SELECT l.* FROM deposit_ledger l JOIN boxes b ON b.id=l.box_id "
            "WHERE b.code=? ORDER BY l.id", (code,))]

    def test_scan_and_deduction_same_box_code(self):
        r = self.issue(self.c1, 1, code="D1")
        code = r["boxes"][0]["box_code"]
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": code, "verdict": "OK"}], W,
                               set_ref="RC-SAME")
        rows = self._ledger_for_box(code)
        self.assertEqual({x["kind"] for x in rows}, {"CHARGE", "REFUND"})
        # 返箱集里的箱号与流水挂账箱号一致
        self.assertEqual(res["accepted"][0]["box_code"], code)
        for x in rows:
            self.assertEqual(x["box_id"], self.box_id(code))

    def test_ok_full_refund_balance(self):
        self.issue(self.c1, 2, code="D2")
        t = self.totals(self.c1)
        self.assertEqual(t["deposit_charged_cents"], 12000)
        self.assertEqual(t["balance_cents"], 12000)
        svc.return_boxes(self.conn, self.fin, self.c1,
                         [{"box_code": "R60-00001", "verdict": "OK"}], W,
                         set_ref="RC-D2")
        t = self.totals(self.c1)
        self.assertEqual(t["refunded_cents"], 6000)
        self.assertEqual(t["balance_cents"], 6000)  # 12000-6000

    def test_repair_deduction_pending_then_confirm(self):
        r = self.issue(self.c1, 1, code="D3")
        code = r["boxes"][0]["box_code"]
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": code, "verdict": "REPAIR",
                                 "body_ok": False}], W, set_ref="RC-D3")
        line = res["accepted"][0]
        self.assertEqual(line["deduct_cents"], 3000)
        self.assertTrue(line["provisional"])
        # 待复核折抵不计入余额，也不进入已折抵
        t = self.totals(self.c1)
        self.assertEqual(t["pending_deduct_cents"], 3000)
        self.assertEqual(t["deducted_cents"], 0)
        self.assertEqual(t["balance_cents"], 6000)

        # 财务确认 25 元 → 余额减 2500
        svc.finance_review(self.conn, self.fin, self.box_id(code), "CONFIRM", F,
                           repair_fee_cents=2500, note="按工单")
        t = self.totals(self.c1)
        self.assertEqual(t["deducted_cents"], 2500)
        self.assertEqual(t["balance_cents"], 3500)
        self.assertEqual(t["pending_deduct_cents"], 0)

    def test_repair_fee_exceeds_deposit_receivable(self):
        r = self.issue(self.c1, 1, typ="R80", code="D4")  # 押金100 维修120
        code = r["boxes"][0]["box_code"]
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": code, "verdict": "REPAIR",
                                 "body_ok": False}], W, set_ref="RC-D4")
        line = res["accepted"][0]
        self.assertEqual(line["deduct_cents"], 10000)
        self.assertEqual(line["receivable_cents"], 2000)
        svc.finance_review(self.conn, self.fin, self.box_id(code), "CONFIRM", F)
        t = self.totals(self.c1)
        self.assertEqual(t["deducted_cents"], 10000)
        self.assertEqual(t["receivable_cents"], 2000)
        self.assertEqual(t["balance_cents"], 0)

    def test_waive_restores_deposit(self):
        r = self.issue(self.c1, 1, code="D5")
        code = r["boxes"][0]["box_code"]
        svc.return_boxes(self.conn, self.fin, self.c1,
                         [{"box_code": code, "verdict": "REPAIR", "body_ok": False}],
                         W, set_ref="RC-D5")
        svc.finance_review(self.conn, self.fin, self.box_id(code), "WAIVE", F,
                           note="客户无责")
        t = self.totals(self.c1)
        self.assertEqual(t["deducted_cents"], 0)
        self.assertEqual(t["balance_cents"], 6000)  # 豁免不扣
        self.assertEqual(t["pending_deduct_cents"], 0)

    def test_scrap_compensation(self):
        r = self.issue(self.c1, 1, code="D6")  # 押金60 采购价90
        code = r["boxes"][0]["box_code"]
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": code, "verdict": "SCRAP",
                                 "body_ok": False, "lid_ok": False}],
                               W, set_ref="RC-D6")
        line = res["accepted"][0]
        self.assertEqual(line["deduct_cents"], 6000)
        self.assertEqual(line["receivable_cents"], 3000)
        svc.finance_review(self.conn, self.fin, self.box_id(code), "CONFIRM", F)
        t = self.totals(self.c1)
        self.assertEqual(t["deducted_cents"], 6000)
        self.assertEqual(t["receivable_cents"], 3000)
        self.assertEqual(t["balance_cents"], 0)

    def test_receipt_totals_match_ledger(self):
        r = self.issue(self.c1, 3, code="D7")
        codes = [x["box_code"] for x in r["boxes"]]
        res = svc.return_boxes(
            self.conn, self.fin, self.c1,
            [{"box_code": codes[0], "verdict": "OK"},
             {"box_code": codes[1], "verdict": "REPAIR", "body_ok": False},
             {"box_code": codes[2], "verdict": "OK"}], W, set_ref="RC-D7")
        # 回执文件存在且合计与库内流水一致
        receipt = json.loads((self.rec / "RC-D7.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["payload"]["totals"], res["totals"])
        self.assertEqual(res["totals"]["refund_cents"], 12000)
        self.assertEqual(res["totals"]["deduct_cents"], 3000)
        self.assertEqual(res["totals"]["count"], 3)


class TestRoles(Base):
    def test_finance_cannot_issue_or_return(self):
        r = self.issue(self.c1, 1, code="R1")
        with self.assertRaises(svc.AuthError):
            svc.issue_boxes(self.conn, self.c1, "R60", 1, "2026-10-20", F, "R1B")
        with self.assertRaises(svc.AuthError):
            svc.return_boxes(self.conn, self.fin, self.c1,
                             [{"box_code": r["boxes"][0]["box_code"], "verdict": "OK"}],
                             F, set_ref="RC-R1")

    def test_warehouse_cannot_review_or_settle(self):
        r = self.issue(self.c1, 1, code="R2")
        code = r["boxes"][0]["box_code"]
        svc.return_boxes(self.conn, self.fin, self.c1,
                         [{"box_code": code, "verdict": "REPAIR", "body_ok": False}],
                         W, set_ref="RC-R2")
        with self.assertRaises(svc.AuthError):
            svc.finance_review(self.conn, self.fin, self.box_id(code), "CONFIRM", W)
        with self.assertRaises(svc.AuthError):
            svc.settle_lost(self.conn, self.fin, self.box_id("R60-00000" if False else code), W)

    def test_admin_everything(self):
        r = svc.issue_boxes(self.conn, self.c1, "R60", 1, "2026-10-20", A, "R3")
        code = r["boxes"][0]["box_code"]
        svc.return_boxes(self.conn, self.fin, self.c1,
                         [{"box_code": code, "verdict": "REPAIR", "body_ok": False}],
                         A, set_ref="RC-R3")
        svc.finance_review(self.conn, self.fin, self.box_id(code), "CONFIRM", A)


class TestRepairAndOverdue(Base):
    def test_repair_done_returns_to_stock(self):
        r = self.issue(self.c1, 1, code="O1")
        code = r["boxes"][0]["box_code"]
        svc.return_boxes(self.conn, self.fin, self.c1,
                         [{"box_code": code, "verdict": "REPAIR", "body_ok": False}],
                         W, set_ref="RC-O1")
        svc.finance_review(self.conn, self.fin, self.box_id(code), "CONFIRM", F)
        svc.repair_done(self.conn, self.box_id(code), W)
        row = self.conn.execute("SELECT state,customer_id FROM boxes WHERE code=?",
                                (code,)).fetchone()
        self.assertEqual(row["state"], "IN_STOCK")
        self.assertIsNone(row["customer_id"])  # 回库后可再分配

    def test_settle_lost_compensation(self):
        # 逾期箱子 R80：押金100 采购价160
        r = self.issue(self.c1, 1, typ="R80", due="2026-09-01", code="O2")
        code = r["boxes"][0]["box_code"]
        overdue = svc.overdue_list(self.conn)
        self.assertTrue(any(x["code"] == code and x["days_overdue"] > 0 for x in overdue))
        res = svc.settle_lost(self.conn, self.fin, self.box_id(code), F)
        self.assertEqual(res["comp_cents"], 10000)
        self.assertEqual(res["receivable_cents"], 6000)
        row = self.conn.execute("SELECT state FROM boxes WHERE code=?", (code,)).fetchone()
        self.assertEqual(row["state"], "LOST")

    def test_settle_then_box_cannot_return(self):
        r = self.issue(self.c1, 1, typ="R80", due="2026-09-01", code="O3")
        code = r["boxes"][0]["box_code"]
        svc.settle_lost(self.conn, self.fin, self.box_id(code), F)
        res = svc.return_boxes(self.conn, self.fin, self.c1,
                               [{"box_code": code, "verdict": "OK"}], W,
                               set_ref="RC-O3")
        self.assertEqual(res["rejected"][0]["reason"], "ALREADY_RETURNED")

    def test_dispute_open_resolve_refund(self):
        r = self.issue(self.c1, 1, code="O4")
        code = r["boxes"][0]["box_code"]
        d = svc.open_dispute(self.conn, self.box_id(code), 6000, F, "重复收押金")
        svc.resolve_dispute(self.conn, self.fin, d["dispute_id"], "核实属实", 6000, F)
        t = self.totals(self.c1)
        self.assertEqual(t["refunded_cents"], 6000)
        self.assertEqual(t["balance_cents"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
