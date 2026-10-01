"""核心领域规则测试：归属 / 重复退箱并发 / 折抵一致性 / 角色限制 / 混批 / 争议冻结。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from toteflow import config, services
from toteflow.db import connect, init_db

WH = {"name": "仓库-王", "role": "warehouse"}
FIN = {"name": "财务-李", "role": "finance"}
ADMIN = {"name": "主管-陈", "role": "admin"}

from toteflow.errors import (Conflict, CrossBatchReturn, DepositDispute,
                             Forbidden, OwnerMismatch)


class ToteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        config.DB_PATH = d / "test.db"
        config.PHOTO_DIR = d / "photos"
        config.ensure_dirs()
        self.conn = connect(config.DB_PATH)
        init_db(self.conn)
        services.add_customer(self.conn, "C001", "甲客户")
        services.add_customer(self.conn, "C002", "乙客户")
        services.add_batch(self.conn, "B1", "批次一")
        services.add_batch(self.conn, "B2", "批次二")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def dispatch(self, no="T-A-0001", cid="C001", batch="B1",
                 typ="标准箱-A", due=30):
        return services.dispatch(self.conn, box_no=no, box_type=typ,
                                 batch_id=batch, cid=cid, actor=WH,
                                 due_days=due)

    # ---------- 箱号归属 ----------
    def test_owner_mismatch_rejected(self):
        self.dispatch("T-A-0001", cid="C001")
        with self.assertRaises(OwnerMismatch):
            services.preview_return(self.conn, box_no="T-A-0001", cid="C002",
                                    return_batch_id=None)
        with self.assertRaises(OwnerMismatch):
            services.accept_return(self.conn, box_no="T-A-0001", cid="C002",
                                   return_batch_id=None,
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH)
        # 箱子仍在外，未产生任何退/扣流水
        state = self.conn.execute(
            "SELECT state FROM boxes WHERE box_no='T-A-0001'").fetchone()["state"]
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM deposit_ledger WHERE box_no='T-A-0001' "
            "AND tx_type<>'CHARGE'").fetchone()["n"]
        self.assertEqual(state, "out")
        self.assertEqual(n, 0)

    def test_unknown_box(self):
        from toteflow.errors import NotFound
        with self.assertRaises(NotFound):
            services.preview_return(self.conn, box_no="NOPE", cid="C001",
                                    return_batch_id=None)

    # ---------- 并发重复退箱：余额只能扣一次 ----------
    def test_concurrent_duplicate_return(self):
        self.dispatch("T-A-0001")
        results, errors = [], []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            c = connect(config.DB_PATH)
            try:
                r = services.accept_return(
                    c, box_no="T-A-0001", cid="C001", return_batch_id=None,
                    body_ok=True, lid_ok=True, label_ok=True, actor=WH)
                results.append(r)
            except Conflict:
                errors.append("conflict")
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, "必须恰好一次退箱成功")
        self.assertEqual(len(errors), 7)
        # 押金只退一次：恰好一笔 REFUND，押金余额守恒
        rows = self.conn.execute(
            "SELECT tx_type, COUNT(*) n, COALESCE(SUM(amount),0) s "
            "FROM deposit_ledger GROUP BY tx_type").fetchall()
        agg = {r["tx_type"]: (r["n"], r["s"]) for r in rows}
        self.assertEqual(agg["CHARGE"], (1, 120.0))
        self.assertEqual(agg["REFUND"], (1, 120.0))
        state = self.conn.execute(
            "SELECT state FROM boxes WHERE box_no='T-A-0001'").fetchone()["state"]
        self.assertEqual(state, "stock")
        n_ret = self.conn.execute(
            "SELECT COUNT(*) n FROM returns WHERE box_no='T-A-0001'").fetchone()["n"]
        self.assertEqual(n_ret, 1, "返箱验收记录也只能有一条")

    # ---------- 折抵一致性：押金 = 退 + 扣（同箱守恒） ----------
    def test_deposit_consistency_good(self):
        self.dispatch("T-A-0001")
        r = services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id=None,
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH)
        self.assertEqual(r["decision"], "ACCEPT")
        self.assertEqual(r["net_refund"], 120.0)
        self._assert_conservation("T-A-0001", 120.0)

    def test_deposit_consistency_repair(self):
        self.dispatch("T-A-0001")
        r = services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id=None,
                                   body_ok=False, lid_ok=True, label_ok=True,
                                   repair_fee=30, actor=WH)
        self.assertEqual(r["decision"], "REPAIR")
        # 验收时只挂预估维修费（待审），尾款维修完成后退
        self.assertEqual(r["deduct"], 30.0)
        self.assertEqual(r["net_refund"], 90.0)
        f = services.finish_repair(self.conn, box_no="T-A-0001",
                                   outcome="repaired", actual_fee=30, actor=WH)
        self.assertEqual(f["outcome"], "repaired")
        # 维修费 30 + 退余额 90 = 押金 120
        led = self.conn.execute(
            "SELECT tx_type, SUM(amount) s FROM deposit_ledger "
            "WHERE box_no='T-A-0001' GROUP BY tx_type").fetchall()
        sums = {r["tx_type"]: r["s"] for r in led}
        self.assertAlmostEqual(sums["DEDUCT_REPAIR"], 30.0, 2)
        self.assertAlmostEqual(sums["REFUND"], 90.0, 2)
        self._assert_conservation("T-A-0001", 120.0)

    def test_deposit_consistency_scrap(self):
        self.dispatch("T-B-0001", typ="高箱-B")  # 押金 180
        r = services.accept_return(self.conn, box_no="T-B-0001", cid="C001",
                                   return_batch_id=None,
                                   body_ok=False, lid_ok=False, label_ok=False,
                                   scrap=True, actor=WH)
        self.assertEqual(r["decision"], "SCRAP")
        self.assertEqual(r["deduct"], 180.0)
        self.assertEqual(r["net_refund"], 0.0)
        self._assert_conservation("T-B-0001", 180.0)
        state = self.conn.execute(
            "SELECT state FROM boxes WHERE box_no='T-B-0001'").fetchone()["state"]
        self.assertEqual(state, "scrapped")

    def _assert_conservation(self, box_no, deposit):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN tx_type='CHARGE' THEN amount END),0) ch, "
            "COALESCE(SUM(CASE WHEN tx_type='REFUND' THEN amount END),0) rf, "
            "COALESCE(SUM(CASE WHEN tx_type LIKE 'DEDUCT%' THEN amount END),0) de "
            "FROM deposit_ledger WHERE box_no=?", (box_no,)).fetchone()
        self.assertAlmostEqual(row["rf"] + row["de"], row["ch"], 2)
        self.assertAlmostEqual(row["ch"], deposit, 2)

    # ---------- 混批（跨批次）返还 ----------
    def test_cross_batch_flow(self):
        self.dispatch("T-A-0001", batch="B1")
        # 仓库扫描，批次不一致 → 拦截
        with self.assertRaises(CrossBatchReturn):
            services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id="B2",
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH)
        # 即使带确认标志，仓库角色也无权确认跨批次
        with self.assertRaises(Forbidden):
            services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id="B2",
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH, allow_cross_batch=True)
        # 财务也不行，只有主管
        with self.assertRaises(Forbidden):
            services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id="B2",
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=FIN, allow_cross_batch=True)
        # 主管确认 → 按实际出库批次 B1 入账
        r = services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id="B2",
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=ADMIN, allow_cross_batch=True)
        self.assertEqual(r["decision"], "ACCEPT")
        rec = self.conn.execute(
            "SELECT batch_id, cross_batch FROM returns WHERE box_no='T-A-0001'"
        ).fetchone()
        self.assertEqual(rec["batch_id"], "B1")
        self.assertEqual(rec["cross_batch"], 1)

    # ---------- 争议冻结抵扣 ----------
    def test_dispute_holds_deposit(self):
        self.dispatch("T-A-0001")
        did = services.open_dispute(self.conn, box_no="T-A-0001",
                                    note="押金金额有异议", actor=FIN)
        with self.assertRaises(DepositDispute):
            services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id=None,
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH)
        services.resolve_dispute(self.conn, dispute_id=did,
                                 note="签收单核对无误", actor=FIN)
        r = services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id=None,
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH)
        self.assertEqual(r["decision"], "ACCEPT")

    # ---------- 角色限制 ----------
    def test_role_restrictions(self):
        self.dispatch("T-A-0001")
        # 财务不能发箱 / 验收
        with self.assertRaises(Forbidden):
            services.dispatch(self.conn, box_no="T-A-0009", box_type="标准箱-A",
                              batch_id="B1", cid="C001", actor=FIN)
        with self.assertRaises(Forbidden):
            services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id=None,
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=FIN)
        # 仓库不能复核折抵
        r = services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                                   return_batch_id=None,
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH)
        lid = self.conn.execute(
            "SELECT id FROM deposit_ledger WHERE box_no='T-A-0001' "
            "AND status='pending'").fetchone()["id"]
        with self.assertRaises(Forbidden):
            services.review_ledger(self.conn, ledger_id=lid, approve=True,
                                   actor=WH)
        # 仓库不能提争议 / 核销长期未返
        with self.assertRaises(Forbidden):
            services.open_dispute(self.conn, box_no="T-A-0001", note="x",
                                  actor=WH)
        self.dispatch("T-A-0002", due=30)
        with self.assertRaises(Forbidden):
            services.declare_loss(self.conn, box_no="T-A-0002", actor=WH)

    def test_finance_review_and_reject(self):
        self.dispatch("T-A-0001")
        services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                               return_batch_id=None,
                               body_ok=True, lid_ok=True, label_ok=True,
                               actor=WH)
        lid = self.conn.execute(
            "SELECT id FROM deposit_ledger WHERE tx_type='REFUND'").fetchone()["id"]
        # 通过后拿到模拟回执，且不能重复复核
        out = services.review_ledger(self.conn, ledger_id=lid, approve=True,
                                     actor=FIN)
        self.assertEqual(out["status"], "confirmed")
        rec = self.conn.execute(
            "SELECT receipt_no,status FROM deposit_ledger WHERE id=?",
            (lid,)).fetchone()
        self.assertEqual(rec["status"], "confirmed")
        self.assertTrue(rec["receipt_no"].startswith("MOCK-RFD-"))
        with self.assertRaises(Conflict):
            services.review_ledger(self.conn, ledger_id=lid, approve=True,
                                   actor=FIN)

    def test_review_reject_opens_dispute(self):
        self.dispatch("T-A-0001")
        services.accept_return(self.conn, box_no="T-A-0001", cid="C001",
                               return_batch_id=None,
                               body_ok=False, lid_ok=True, label_ok=True,
                               repair_fee=50, actor=WH)
        lid = self.conn.execute(
            "SELECT id FROM deposit_ledger WHERE tx_type='DEDUCT_REPAIR'"
        ).fetchone()["id"]
        with self.assertRaises(services.ToteError):
            services.review_ledger(self.conn, ledger_id=lid, approve=False,
                                   actor=FIN)  # 驳回必须填原因
        services.review_ledger(self.conn, ledger_id=lid, approve=False,
                               actor=FIN, note="维修费报价虚高")
        st = self.conn.execute("SELECT status FROM deposit_ledger WHERE id=?",
                               (lid,)).fetchone()["status"]
        self.assertEqual(st, "rejected")
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM disputes WHERE box_no='T-A-0001' "
            "AND status='open'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_overdue_loss_deducts_deposit(self):
        self.dispatch("T-A-0001", due=30)
        self.conn.execute(
            "UPDATE boxes SET due_at=datetime('now','localtime','-10 day') "
            "WHERE box_no='T-A-0001'")
        from toteflow import queries
        ov = queries.overdue_boxes(self.conn, days=30)
        self.assertEqual([b["box_no"] for b in ov], ["T-A-0001"])
        services.declare_loss(self.conn, box_no="T-A-0001", actor=FIN,
                              note="客户停业，追箱无果")
        state = self.conn.execute(
            "SELECT state FROM boxes WHERE box_no='T-A-0001'").fetchone()["state"]
        self.assertEqual(state, "lost")
        self._assert_conservation("T-A-0001", 120.0)

    def test_dispatch_duplicate_box_no(self):
        self.dispatch("T-A-0001")
        with self.assertRaises(Conflict):
            self.dispatch("T-A-0001")


if __name__ == "__main__":
    unittest.main()
