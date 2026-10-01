"""领域服务：发箱 / 返箱验收 / 维修池 / 争议 / 押金折抵 / 财务复核。

关键一致性约束：
1. 返箱扫描与押金抵扣绑定同一 box_no，写在同一事务里；
2. 状态迁移用条件 UPDATE（... WHERE state='out'），BEGIN IMMEDIATE 下
   并发退箱至多一个成功，余额不可能扣两次；
3. 押金流水不可变，冲正/改判由财务复核落新状态，仓库无写流水权限。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import config
from .adapters import gateway
from .db import log_event, tx
from .errors import (Conflict, CrossBatchReturn, DepositDispute, Forbidden,
                     NotFound, OwnerMismatch, ToteError)

# ---------------------------------------------------------------- 角色

_ROLE_HIERARCHY = {
    "dispatch": {"warehouse", "admin"},
    "inspect": {"warehouse", "admin"},
    "repair": {"warehouse", "admin"},
    "finance": {"finance", "admin"},
    "admin": {"admin"},
}


def require(role: str, action: str) -> None:
    if role not in _ROLE_HIERARCHY[action]:
        raise Forbidden(f"{config.ROLE_NAMES.get(role, role)}无权执行该操作")


# ---------------------------------------------------------------- 主数据

def add_customer(conn, cid: str, name: str, contact: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO customers(cid,name,contact) VALUES (?,?,?)",
        (cid, name, contact),
    )


def add_batch(conn, bid: str, note: str = "") -> None:
    conn.execute("INSERT OR IGNORE INTO batches(bid,note) VALUES (?,?)", (bid, note))


# ---------------------------------------------------------------- 发箱扫描

def dispatch(conn, *, box_no: str, box_type: str, batch_id: str, cid: str,
             actor: dict, due_days: int = config.OVERDUE_DAYS) -> dict:
    """工厂发货：一箱一事件，同时记一笔押金收取（confirmed）。"""
    require(actor["role"], "dispatch")
    if box_type not in config.DEPOSIT_BY_TYPE:
        raise ToteError(f"未知箱型：{box_type}")
    deposit = config.DEPOSIT_BY_TYPE[box_type]

    with tx(conn):
        if conn.execute("SELECT 1 FROM boxes WHERE box_no=?", (box_no,)).fetchone():
            raise Conflict(f"箱号 {box_no} 已存在，不能重复发箱")
        if not conn.execute("SELECT 1 FROM customers WHERE cid=?", (cid,)).fetchone():
            raise NotFound(f"客户不存在：{cid}")
        if not conn.execute("SELECT 1 FROM batches WHERE bid=?", (batch_id,)).fetchone():
            raise NotFound(f"批次不存在：{batch_id}")

        now = datetime.now()
        due = (now + timedelta(days=due_days)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO boxes(box_no,box_type,deposit,batch_id,holder_cid,state,
                                 dispatched_at,due_at)
               VALUES (?,?,?,?,?,'out',?,?)""",
            (box_no, box_type, deposit, batch_id, cid,
             now.strftime("%Y-%m-%d %H:%M:%S"), due),
        )
        conn.execute(
            """INSERT INTO deposit_ledger(box_no,cid,batch_id,tx_type,amount,status,
                                          reason,created_by,receipt_no)
               VALUES (?,?,?,'CHARGE',?,'confirmed','发箱收取押金',?,?)""",
            (box_no, cid, batch_id, deposit, actor["name"],
             gateway.submit_refund(box_no=box_no, cid=cid, amount=deposit,
                                   ref=f"CHG-{batch_id}")),
        )
        log_event(conn, box_no, "DISPATCH", actor["name"],
                  {"batch_id": batch_id, "cid": cid, "deposit": deposit})
    return {"box_no": box_no, "deposit": deposit}


# ---------------------------------------------------------------- 返箱扫描预览

def preview_return(conn, *, box_no: str, cid: str, return_batch_id: str | None) -> dict:
    """只校验不改账：归属、状态、跨批次、争议。"""
    box = conn.execute("SELECT * FROM boxes WHERE box_no=?", (box_no,)).fetchone()
    if not box:
        raise NotFound(f"查无此箱：{box_no}")
    cust = conn.execute("SELECT name FROM customers WHERE cid=?", (cid,)).fetchone()
    if not cust:
        raise NotFound(f"客户不存在：{cid}")

    result = dict(box)
    result["holder_name"] = cust["name"]
    result["owner_ok"] = box["holder_cid"] == cid and box["state"] == "out"
    result["already"] = box["state"] != "out"
    result["cross_batch"] = (
        return_batch_id is not None and box["batch_id"] != return_batch_id
    )
    result["dispute_open"] = bool(conn.execute(
        "SELECT 1 FROM disputes WHERE box_no=? AND status='open'", (box_no,)).fetchone())

    if result["already"]:
        state_cn = {"stock": "已入库", "repair": "维修中",
                    "scrapped": "已报废", "lost": "已核销"}.get(box["state"], box["state"])
        raise Conflict(f"箱号 {box_no} 当前为「{state_cn}」，不能重复退箱")
    if not result["owner_ok"]:
        owner = conn.execute("SELECT name FROM customers WHERE cid=?",
                             (box["holder_cid"],)).fetchone()
        owner_name = owner["name"] if owner else box["holder_cid"]
        raise OwnerMismatch(
            f"箱号 {box_no} 不属于客户 {cust['name']}，实际归属：{owner_name}")
    return result


# ---------------------------------------------------------------- 返箱验收

def _add_ledger(conn, *, box_no, cid, batch_id, tx_type, amount, reason, actor):
    cur = conn.execute(
        """INSERT INTO deposit_ledger(box_no,cid,batch_id,tx_type,amount,status,
                                      reason,created_by)
           VALUES (?,?,?,?,?,'pending',?,?)""",
        (box_no, cid, batch_id, tx_type, amount, reason, actor["name"]),
    )
    return cur.lastrowid


def accept_return(conn, *, box_no: str, cid: str, return_batch_id: str | None,
                  body_ok: bool, lid_ok: bool, label_ok: bool,
                  actor: dict, allow_cross_batch: bool = False,
                  repair_fee: float = 0.0, scrap: bool = False,
                  fault: str = "",
                  photo_ids: list[int] | None = None) -> dict:
    """验收并入账。所有判定与押金写在同一 BEGIN IMMEDIATE 事务。"""
    require(actor["role"], "inspect")
    fee = round(max(0.0, float(repair_fee)), 2)
    if not scrap and fee > config.REPAIR_FEE_CAP:
        raise ToteError(f"维修费用 {fee} 超过封顶 {config.REPAIR_FEE_CAP}")

    with tx(conn):
        pre = preview_return(conn, box_no=box_no, cid=cid,
                             return_batch_id=return_batch_id)
        if pre["dispute_open"]:
            raise DepositDispute(
                f"箱号 {box_no} 存在未决押金争议，已冻结抵扣，转争议处理")
        if pre["cross_batch"] and not allow_cross_batch:
            log_event(conn, box_no, "CROSS_BATCH", actor["name"],
                      {"box_batch": pre["batch_id"],
                       "return_batch": return_batch_id})
            raise CrossBatchReturn(
                f"箱号 {box_no} 属批次 {pre['batch_id']}，与返还批次 "
                f"{return_batch_id} 不一致，需主管确认跨批次返还")
        # 跨批次确认仅主管可执行
        if pre["cross_batch"] and allow_cross_batch:
            require(actor["role"], "admin")

        # 三项质检
        ok = bool(body_ok and lid_ok and label_ok)
        if scrap and ok:
            raise ToteError("三项质检均合格，不能判报废")
        defects = [n for n, v in (("箱体", body_ok), ("封盖", lid_ok),
                                  ("标签", label_ok)) if not v]

        deposit = pre["deposit"]
        deduct_id = refund_id = None
        new_state = "stock"
        decision = "ACCEPT"
        reason = "质检合格"
        photos = photo_ids or []

        if ok:
            decision = "ACCEPT"
            new_state = "stock"
            refund_id = _add_ledger(
                conn, box_no=box_no, cid=cid, batch_id=pre["batch_id"],
                tx_type="REFUND", amount=deposit, reason="验收合格，押金全额退还",
                actor=actor)
        elif scrap or fee >= deposit:
            # 严重破损无修复价值（或费用达到押金额）→ 报废折抵全额押金
            decision = "SCRAP"
            new_state = "scrapped"
            reason = f"破损报废（{'、'.join(defects)}不合格）"
            deduct_id = _add_ledger(
                conn, box_no=box_no, cid=cid, batch_id=pre["batch_id"],
                tx_type="DEDUCT_LOSS", amount=deposit,
                reason=f"报废折抵押金 {deposit:.2f}（{reason}）", actor=actor)
            fee = 0.0
        else:
            decision = "REPAIR"
            new_state = "repair"
            reason = f"待修（{'、'.join(defects)}不合格）"
            if fee > 0:
                deduct_id = _add_ledger(
                    conn, box_no=box_no, cid=cid, batch_id=pre["batch_id"],
                    tx_type="DEDUCT_REPAIR", amount=fee,
                    reason=f"维修费折抵押金（预估 {fee:.2f}）", actor=actor)

        # —— 原子状态迁移：只有 state='out' 才成功，并发退箱的第二写者必败 ——
        cur = conn.execute(
            "UPDATE boxes SET state=?, holder_cid=NULL, updated_at=datetime('now','localtime') "
            "WHERE box_no=? AND state='out'",
            (new_state, box_no),
        )
        if cur.rowcount == 0:
            # 不该发生（事务内已预览），但作为重复退箱的最终防线
            raise Conflict(f"箱号 {box_no} 已被处理，禁止重复退箱")

        ret_id = conn.execute(
            """INSERT INTO returns(box_no,expect_cid,actual_cid,batch_id,inspector,
                                   body_ok,lid_ok,label_ok,owner_ok,cross_batch,
                                   decision,repair_fee,reason,refund_id,deduct_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (box_no, cid, pre["holder_cid"], pre["batch_id"], actor["name"],
             int(bool(body_ok)), int(bool(lid_ok)), int(bool(label_ok)), 1,
             int(pre["cross_batch"]), decision, fee, reason, refund_id, deduct_id),
        ).lastrowid

        if decision == "REPAIR":
            conn.execute(
                """INSERT INTO repairs(box_no,opened_by,fee,fault,photo_ids)
                   VALUES (?,?,?,?,?)""",
                (box_no, actor["name"], fee, fault or reason,
                 json.dumps(photos)))

        log_event(conn, box_no,
                  "RETURN_ACCEPT" if ok else ("SCRAP" if decision == "SCRAP"
                                              else "RETURN_ACCEPT"),
                  actor["name"],
                  {"decision": decision, "defects": defects, "fee": fee,
                   "cross_batch": pre["cross_batch"], "return_id": ret_id,
                   "refund_id": refund_id, "deduct_id": deduct_id})

    return {
        "box_no": box_no, "decision": decision, "state": new_state,
        "deposit": deposit, "repair_fee": fee,
        "refund": round(deposit if decision == "ACCEPT" else 0.0, 2),
        "deduct": fee if decision == "REPAIR" else
                  (deposit if decision == "SCRAP" else 0.0),
        "net_refund": round(
            deposit if decision == "ACCEPT" else
            (deposit - fee if decision == "REPAIR" else 0.0), 2),
        "reason": reason,
    }


# ---------------------------------------------------------------- 维修池

def finish_repair(conn, *, box_no: str, outcome: str, actual_fee: float | None,
                  actor: dict) -> dict:
    """维修闭环：repaired→重新入库；scrapped→报废。维修费与尾款在此结清。"""
    require(actor["role"], "repair")
    rep = conn.execute("SELECT * FROM repairs WHERE box_no=?", (box_no,)).fetchone()
    if not rep:
        raise NotFound(f"维修池无此箱：{box_no}")
    if rep["closed_at"]:
        raise Conflict(f"箱号 {box_no} 维修单已关闭")
    box = conn.execute("SELECT * FROM boxes WHERE box_no=?", (box_no,)).fetchone()
    if box["state"] != "repair":
        raise Conflict(f"箱号 {box_no} 不在维修状态")

    fee = round(float(rep["fee"] if actual_fee is None else actual_fee), 2)
    if fee > config.REPAIR_FEE_CAP:
        raise ToteError(f"维修费用 {fee} 超过封顶 {config.REPAIR_FEE_CAP}")

    with tx(conn):
        row = conn.execute(
            "SELECT deduct_id, actual_cid AS cid, batch_id FROM returns "
            "WHERE box_no=? ORDER BY id DESC LIMIT 1", (box_no,)).fetchone()
        cid, batch_id = row["cid"], row["batch_id"]

        if outcome == "repaired":
            new_state = "stock"
            if fee > 0 and row["deduct_id"]:
                rec = conn.execute(
                    "SELECT status FROM deposit_ledger WHERE id=?",
                    (row["deduct_id"],)).fetchone()
                if rec["status"] == "pending":
                    conn.execute(
                        "UPDATE deposit_ledger SET amount=?, reason=? WHERE id=?",
                        (fee, f"维修费折抵押金（实际 {fee:.2f}）",
                         row["deduct_id"]))
                else:
                    raise Conflict("维修费流水已被财务复核，不能再改金额")
            refund = round(box["deposit"] - fee, 2)
            refund_id = None
            if refund > 0:
                refund_id = _add_ledger(
                    conn, box_no=box_no, cid=cid, batch_id=batch_id,
                    tx_type="REFUND", amount=refund,
                    reason=f"维修完成重新入库，退还押金余额 {refund:.2f}",
                    actor=actor)
            conn.execute(
                "UPDATE returns SET repair_fee=? WHERE box_no=? AND decision='REPAIR'",
                (fee, box_no))
            conn.execute(
                "UPDATE repairs SET fee=?, closed_at=datetime('now','localtime'), "
                "outcome='repaired' WHERE box_no=?", (fee, box_no))
            log_event(conn, box_no, "REPAIR_DONE", actor["name"],
                      {"fee": fee, "refund": refund})
        elif outcome == "scrapped":
            new_state = "scrapped"
            loss = round(box["deposit"] - fee, 2)
            loss_id = None
            if loss > 0:
                loss_id = _add_ledger(
                    conn, box_no=box_no, cid=cid, batch_id=batch_id,
                    tx_type="DEDUCT_LOSS", amount=loss,
                    reason=f"维修无果报废，补缴押金损失 {loss:.2f}", actor=actor)
            conn.execute(
                "UPDATE repairs SET fee=?, closed_at=datetime('now','localtime'), "
                "outcome='scrapped' WHERE box_no=?", (fee, box_no))
            log_event(conn, box_no, "SCRAP", actor["name"],
                      {"fee": fee, "loss": loss, "loss_id": loss_id})
        else:
            raise ToteError("outcome 只能是 repaired / scrapped")

        cur = conn.execute(
            "UPDATE boxes SET state=? WHERE box_no=? AND state='repair'",
            (new_state, box_no))
        if cur.rowcount == 0:
            raise Conflict(f"箱号 {box_no} 状态已变更，操作失效")

    return {"box_no": box_no, "outcome": outcome, "fee": fee}


# ---------------------------------------------------------------- 押金争议

def open_dispute(conn, *, box_no: str, note: str, actor: dict) -> int:
    require(actor["role"], "finance")
    box = conn.execute("SELECT holder_cid,state FROM boxes WHERE box_no=?",
                       (box_no,)).fetchone()
    if not box:
        raise NotFound(f"查无此箱：{box_no}")
    if conn.execute("SELECT 1 FROM disputes WHERE box_no=? AND status='open'",
                    (box_no,)).fetchone():
        raise Conflict("该箱已有未决争议")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO disputes(box_no,cid,note,opened_by) VALUES (?,?,?,?)",
            (box_no, box["holder_cid"] or "", note, actor["name"]))
        log_event(conn, box_no, "HOLD", actor["name"], {"note": note})
        return cur.lastrowid


def resolve_dispute(conn, *, dispute_id: int, note: str, actor: dict) -> None:
    require(actor["role"], "finance")
    with tx(conn):
        cur = conn.execute(
            "UPDATE disputes SET status='resolved', resolved_by=?, "
            "resolved_at=datetime('now','localtime'), "
            "note=note||' || 解决：'||? WHERE id=? AND status='open'",
            (actor["name"], note, dispute_id))
        if cur.rowcount == 0:
            raise NotFound("争议不存在或已解决")
        box = conn.execute("SELECT box_no FROM disputes WHERE id=?",
                           (dispute_id,)).fetchone()
        log_event(conn, box["box_no"], "RELEASE", actor["name"], {"note": note})


# ---------------------------------------------------------------- 财务复核

def review_ledger(conn, *, ledger_id: int, approve: bool, actor: dict,
                  note: str = "") -> dict:
    """财务复核待审折抵/退款；仓库无此权限。驳回需写明原因并挂争议。"""
    require(actor["role"], "finance")
    with tx(conn):
        row = conn.execute(
            "SELECT * FROM deposit_ledger WHERE id=?", (ledger_id,)).fetchone()
        if not row:
            raise NotFound("流水不存在")
        if row["status"] != "pending":
            raise Conflict(f"流水已复核（{row['status']}），不能重复操作")
        if approve:
            receipt = (gateway.submit_refund
                       if row["tx_type"] == "REFUND"
                       else gateway.submit_deduction)(
                box_no=row["box_no"], cid=row["cid"], amount=row["amount"],
                ref=f"LDG-{ledger_id}")
            conn.execute(
                "UPDATE deposit_ledger SET status='confirmed', reviewed_by=?, "
                "reviewed_at=datetime('now','localtime'), receipt_no=?, "
                "reason=reason||? WHERE id=?",
                (actor["name"], receipt,
                 f"（财务复核通过 {note}）" if note else "（财务复核通过）",
                 ledger_id))
        else:
            if not note:
                raise ToteError("驳回复核必须填写原因")
            conn.execute(
                "UPDATE deposit_ledger SET status='rejected', reviewed_by=?, "
                "reviewed_at=datetime('now','localtime'), "
                "reason=reason||'（复核驳回：'||?||'）' WHERE id=?",
                (actor["name"], note, ledger_id))
            conn.execute(
                "INSERT INTO disputes(box_no,cid,note,opened_by) VALUES (?,?,?,?)",
                (row["box_no"], row["cid"],
                 f"流水#{ledger_id} 复核驳回：{note}", actor["name"]))
            log_event(conn, row["box_no"], "HOLD", actor["name"],
                      {"ledger_id": ledger_id, "note": note})
        return {"id": ledger_id, "status": "confirmed" if approve else "rejected"}


# ---------------------------------------------------------------- 长期未返 / 核销

def declare_loss(conn, *, box_no: str, actor: dict, note: str = "") -> dict:
    """长期未返且追不回：财务/主管核销，押金全额扣抵。"""
    require(actor["role"], "finance")
    with tx(conn):
        box = conn.execute(
            "SELECT * FROM boxes WHERE box_no=? AND state='out'",
            (box_no,)).fetchone()
        if not box:
            raise Conflict("仅在外未返的箱子可核销")
        conn.execute(
            "UPDATE boxes SET state='lost', updated_at=datetime('now','localtime') "
            "WHERE box_no=? AND state='out'", (box_no,))
        conn.execute(
            """INSERT INTO deposit_ledger(box_no,cid,batch_id,tx_type,amount,status,
                          reason,created_by)
               VALUES (?,?,?,'DEDUCT_LOSS',?,'pending',?,?)""",
            (box_no, box["holder_cid"], box["batch_id"], box["deposit"],
             f"长期未返核销，押金扣抵（{note}）" if note else "长期未返核销，押金扣抵",
             actor["name"]))
        log_event(conn, box_no, "SCRAP", actor["name"], {"kind": "loss", "note": note})
    return {"box_no": box_no, "deduct": box["deposit"]}
