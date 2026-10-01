"""核心领域服务：发箱、返箱验收、维修/报废、折抵、复核、逾期核销、争议。

并发安全：退箱扣押金用 `UPDATE boxes SET ... WHERE id=? AND state='OUT'`
做状态条件更新，配合 BEGIN IMMEDIATE 立即写锁，两个并发退箱请求只有一个
能把箱子从 OUT 迁走，另一个得到 ALREADY_RETURNED，余额不可能扣两次。

角色：
  warehouse 仓库 —— 只负责实物（发箱、验收结论、照片、修完回库）
  finance   财务 —— 折抵复核、逾期核销、押金争议、流水只读
  admin     管理员 —— 全部
金额一律以整数“分”存储。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, date

from .finance import MockFinanceAdapter

# ---- 角色与权限 -----------------------------------------------------------
ROLE_WAREHOUSE = "warehouse"
ROLE_FINANCE = "finance"
ROLE_ADMIN = "admin"
ROLES = (ROLE_WAREHOUSE, ROLE_FINANCE, ROLE_ADMIN)
ROLE_LABELS = {
    ROLE_WAREHOUSE: "仓库",
    ROLE_FINANCE: "财务",
    ROLE_ADMIN: "管理员",
}

# 仓库负责实物状态；折抵复核 / 核销 / 争议只归财务
_PHYSICAL_ACTIONS = {"issue", "return", "repair_done"}
_FINANCE_ACTIONS = {"review", "settle_lost", "dispute_open", "dispute_resolve"}


class DomainError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class AuthError(DomainError):
    def __init__(self, action: str, role: str):
        super().__init__(
            "FORBIDDEN", f"角色“{ROLE_LABELS.get(role, role)}”无权执行该操作（{action}）", 403
        )


def require_role(action: str, role: str) -> None:
    if role == ROLE_ADMIN:
        return
    if action in _PHYSICAL_ACTIONS and role != ROLE_WAREHOUSE:
        raise AuthError(action, role)
    if action in _FINANCE_ACTIONS and role != ROLE_FINANCE:
        raise AuthError(action, role)


def cny(cents: int | None) -> str:
    return f"¥{(cents or 0) / 100:.2f}"


def yuan_to_cents(v) -> int:
    return int(round(float(v) * 100))


def _new_ref(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"


# ---- 基础资料 -------------------------------------------------------------
def create_customer(conn: sqlite3.Connection, code: str, name: str) -> int:
    cur = conn.execute("INSERT INTO customers(code,name) VALUES(?,?)", (code, name))
    return cur.lastrowid


def create_box_type(
    conn: sqlite3.Connection, code: str, name: str,
    deposit_cents: int, repair_cents: int, price_cents: int,
) -> None:
    conn.execute(
        "INSERT INTO box_types(code,name,deposit_cents,repair_cents,price_cents)"
        " VALUES(?,?,?,?,?)",
        (code, name, deposit_cents, repair_cents, price_cents),
    )


def list_box_types(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM box_types ORDER BY code")]


def list_customers(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id,code,name FROM customers ORDER BY code")]


# ---- 发箱 -----------------------------------------------------------------
def _allocate_codes(conn: sqlite3.Connection, type_code: str, qty: int) -> list[str]:
    """优先复用库内同箱型空箱；不足则按顺序号生成新箱号。"""
    reused = [r["code"] for r in conn.execute(
        "SELECT code FROM boxes WHERE type_code=? AND state='IN_STOCK' ORDER BY id LIMIT ?",
        (type_code, qty),
    )]
    need = qty - len(reused)
    codes = list(reused)
    if need > 0:
        row = conn.execute(
            "SELECT next_no FROM code_seq WHERE type_code=?", (type_code,)
        ).fetchone()
        start = row["next_no"] if row else 1
        if row:
            conn.execute("UPDATE code_seq SET next_no=? WHERE type_code=?",
                         (start + need, type_code))
        else:
            conn.execute("INSERT INTO code_seq(type_code,next_no) VALUES(?,?)",
                         (type_code, start + need))
        codes += [f"{type_code}-{n:05d}" for n in range(start, start + need)]
        conn.executemany(
            "INSERT INTO boxes(code,type_code,state) VALUES(?,?,'IN_STOCK')",
            [(c, type_code) for c in codes[len(reused):]],
        )
    return codes


def issue_boxes(
    conn: sqlite3.Connection, customer_id: int, type_code: str, qty: int,
    due_at: str, actor_role: str = ROLE_WAREHOUSE, batch_code: str | None = None,
) -> dict:
    """工厂发货：登记出库批次、把箱号分配给客户、逐箱收押金。"""
    require_role("issue", actor_role)
    if qty <= 0:
        raise DomainError("BAD_QTY", "发货数量必须大于 0")
    cust = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not cust:
        raise DomainError("NO_CUSTOMER", "客户不存在", 404)
    bt = conn.execute("SELECT * FROM box_types WHERE code=?", (type_code,)).fetchone()
    if not bt:
        raise DomainError("NO_TYPE", "箱型不存在", 404)
    try:
        date.fromisoformat(due_at)
    except ValueError:
        raise DomainError("BAD_DATE", "应返期限格式应为 YYYY-MM-DD")

    conn.execute("BEGIN IMMEDIATE")
    try:
        code = batch_code or _new_ref("B")
        dup = conn.execute("SELECT 1 FROM outbound_batches WHERE code=?", (code,)).fetchone()
        if dup:
            raise DomainError("BATCH_DUP", f"出库批次号 {code} 已存在", 409)
        cur = conn.execute(
            "INSERT INTO outbound_batches(code,customer_id,type_code,qty,due_at)"
            " VALUES(?,?,?,?,?)",
            (code, customer_id, type_code, qty, due_at),
        )
        batch_id = cur.lastrowid
        box_codes = _allocate_codes(conn, type_code, qty)
        rows = []
        for bc in box_codes:
            box = conn.execute("SELECT * FROM boxes WHERE code=?", (bc,)).fetchone()
            conn.execute(
                "UPDATE boxes SET state='OUT',customer_id=?,batch_id=?,"
                "out_at=datetime('now'),due_at=?,returned_at=NULL,"
                "disposition=NULL,photo_path=NULL,note=NULL WHERE id=?",
                (customer_id, batch_id, due_at, box["id"]),
            )
            conn.execute(
                "INSERT INTO box_events(box_id,box_code,customer_id,batch_id,event,actor_role,detail)"
                " VALUES(?,?,?,?, 'ISSUE',?,?)",
                (box["id"], bc, customer_id, batch_id, actor_role,
                 json.dumps({"batch": code, "type": type_code}, ensure_ascii=False)),
            )
            ref = f"CHG-{batch_id}-{box['id']}"
            conn.execute(
                "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note)"
                " VALUES(?,?,?,?, 'CHARGE',?,?)",
                (ref, box["id"], customer_id, batch_id, bt["deposit_cents"],
                 f"发箱押金 {bc}（批次 {code}）"),
            )
            rows.append({"box_code": bc, "deposit_cents": bt["deposit_cents"]})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"batch_code": code, "batch_id": batch_id, "boxes": rows,
            "deposit_total_cents": bt["deposit_cents"] * qty}


# ---- 返箱验收 -------------------------------------------------------------
def _log_exception(conn, kind, box_code, detail, role):
    conn.execute(
        "INSERT INTO exceptions(kind,box_code,detail,actor_role) VALUES(?,?,?,?)",
        (kind, box_code, detail, role),
    )


def return_boxes(
    conn: sqlite3.Connection, finance: MockFinanceAdapter, customer_id: int,
    items: list[dict], actor_role: str = ROLE_WAREHOUSE,
    expected_batch_id: int | None = None, allow_cross_batch: bool = False,
    set_ref: str | None = None,
) -> dict:
    """扫码返箱验收并即时折抵。

    items: [{box_code, verdict: OK|REPAIR|SCRAP, body_ok, lid_ok, label_ok,
             photo_path, note}]
    验收结论与折抵在同一事务、同一箱号上落账，并出具一张整批财务回执。
    """
    require_role("return", actor_role)
    if not items:
        raise DomainError("EMPTY", "没有需要验收的箱号")
    cust = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not cust:
        raise DomainError("NO_CUSTOMER", "客户不存在", 404)

    set_ref = set_ref or _new_ref("RC")
    prior = conn.execute(
        "SELECT payload FROM finance_receipts WHERE ref=?", (set_ref,)
    ).fetchone()
    if prior:  # 幂等：同一返箱集 key 重放，返回首次结果，绝不重复扣款
        return json.loads(prior["payload"])

    conn.execute("BEGIN IMMEDIATE")
    try:
        accepted, rejected = [], []
        for it in items:
            bc = (it.get("box_code") or "").strip()
            box = conn.execute("SELECT * FROM boxes WHERE code=?", (bc,)).fetchone()
            if not box:
                rejected.append({"box_code": bc, "reason": "BOX_NOT_FOUND",
                                 "message": "箱号不存在"})
                _log_exception(conn, "NOT_OWNED", bc, "箱号不存在", actor_role)
                continue
            # 箱号归属：必须是该客户当前持有的箱子
            if box["customer_id"] != customer_id or box["state"] != "OUT":
                if box["state"] != "OUT":
                    reason, msg = "ALREADY_RETURNED", "该箱已返还或不在出库状态，禁止重复退箱"
                    kind = "ALREADY_RETURNED"
                else:
                    owner = conn.execute(
                        "SELECT name FROM customers WHERE id=?", (box["customer_id"],)
                    ).fetchone()
                    reason, msg = "NOT_OWNED", f"箱号不属于该客户（归属：{owner['name'] if owner else '—'}）"
                    kind = "NOT_OWNED"
                rejected.append({"box_code": bc, "reason": reason, "message": msg})
                _log_exception(conn, kind, bc, msg, actor_role)
                continue
            # 跨批次：默认按选定批次核对；未指定批次时允许混批但逐箱标注
            cross = False
            if expected_batch_id is not None and box["batch_id"] != expected_batch_id:
                if not allow_cross_batch:
                    rejected.append({"box_code": bc, "reason": "CROSS_BATCH",
                                     "message": "跨批次返还，需勾选“允许混批”后放行"})
                    _log_exception(conn, "CROSS_BATCH", bc,
                                   f"实际批次 {box['batch_id']}，期望 {expected_batch_id}", actor_role)
                    continue
                cross = True
            elif expected_batch_id is None:
                cross = True  # 未指定批次的混扫，前端按批次分组提示

            body_ok = bool(it.get("body_ok", True))
            lid_ok = bool(it.get("lid_ok", True))
            label_ok = bool(it.get("label_ok", True))
            verdict = it.get("verdict") or ("OK" if (body_ok and lid_ok and label_ok) else "REPAIR")
            if verdict not in ("OK", "REPAIR", "SCRAP"):
                raise DomainError("BAD_VERDICT", f"{bc} 验收结论非法")
            bt = conn.execute("SELECT * FROM box_types WHERE code=?",
                              (box["type_code"],)).fetchone()
            batch = conn.execute("SELECT code FROM outbound_batches WHERE id=?",
                                 (box["batch_id"],)).fetchone()
            photo = it.get("photo_path")

            # 关键原子步骤：只有仍是 OUT 的箱子才能迁走 → 并发退箱只有一个成功
            if verdict == "OK":
                cur = conn.execute(
                    "UPDATE boxes SET state='IN_STOCK',returned_at=datetime('now'),"
                    "disposition='OK',photo_path=?,note=? "
                    "WHERE id=? AND state='OUT'",
                    (photo, it.get("note"), box["id"]),
                )
                new_state, event_name = "IN_STOCK", "RETURN_OK"
            elif verdict == "REPAIR":
                cur = conn.execute(
                    "UPDATE boxes SET state='REPAIR',returned_at=datetime('now'),"
                    "disposition='REPAIR',photo_path=?,note=? "
                    "WHERE id=? AND state='OUT'",
                    (photo, it.get("note"), box["id"]),
                )
                new_state, event_name = "REPAIR", "RETURN_REPAIR"
            else:
                cur = conn.execute(
                    "UPDATE boxes SET state='SCRAPPED',returned_at=datetime('now'),"
                    "disposition='SCRAP',photo_path=?,note=? "
                    "WHERE id=? AND state='OUT'",
                    (photo, it.get("note"), box["id"]),
                )
                new_state, event_name = "SCRAPPED", "RETURN_SCRAP"
            if cur.rowcount == 0:  # 并发下已被另一个请求返还
                rejected.append({"box_code": bc, "reason": "ALREADY_RETURNED",
                                 "message": "并发冲突：该箱刚刚已被返还"})
                _log_exception(conn, "ALREADY_RETURNED", bc, "并发退箱冲突", actor_role)
                continue

            detail = {
                "batch": batch["code"], "cross_batch": cross,
                "body_ok": body_ok, "lid_ok": lid_ok, "label_ok": label_ok,
                "photo": photo, "note": it.get("note", ""),
                "inspection": {"箱体": body_ok, "封盖": lid_ok, "标签": label_ok},
            }
            conn.execute(
                "INSERT INTO box_events(box_id,box_code,customer_id,batch_id,event,actor_role,detail)"
                " VALUES(?,?,?,?,?,?,?)",
                (box["id"], bc, customer_id, box["batch_id"], event_name, actor_role,
                 json.dumps(detail, ensure_ascii=False)),
            )

            # —— 折抵与箱号一一对应 ——
            line = {
                "box_code": bc, "type_code": box["type_code"],
                "batch_code": batch["code"], "cross_batch": cross,
                "verdict": verdict, "state": new_state, "photo_path": photo,
                "deposit_cents": bt["deposit_cents"],
                "deduct_cents": 0, "receivable_cents": 0,
                "refund_cents": 0, "provisional": False,
            }
            if verdict == "OK":
                ref = f"{set_ref}-{box['id']}-RFD"
                conn.execute(
                    "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note)"
                    " VALUES(?,?,?,?, 'REFUND',?,?)",
                    (ref, box["id"], customer_id, box["batch_id"], bt["deposit_cents"],
                     f"验收入库退押金 {bc}（批次 {batch['code']}）"),
                )
                line["refund_cents"] = bt["deposit_cents"]
            elif verdict == "REPAIR":
                fee = bt["repair_cents"]
                deduct, rcvb = min(bt["deposit_cents"], fee), max(0, fee - bt["deposit_cents"])
                conn.execute(
                    "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note,provisional)"
                    " VALUES(?,?,?,?, 'REPAIR_DEDUCT',?,?,1)",
                    (f"{set_ref}-{box['id']}-RP", box["id"], customer_id, box["batch_id"],
                     deduct, f"破损维修抵扣 {bc}（待财务复核）"),
                )
                if rcvb:
                    conn.execute(
                        "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note,provisional)"
                        " VALUES(?,?,?,?, 'REPAIR_RECEIVABLE',?,?,1)",
                        (f"{set_ref}-{box['id']}-RPR", box["id"], customer_id, box["batch_id"],
                         rcvb, f"维修费超出押金应收 {bc}（待确认）"),
                    )
                line.update(deduct_cents=deduct, receivable_cents=rcvb, provisional=True)
            else:  # SCRAP
                comp = min(bt["deposit_cents"], bt["price_cents"])
                rcvb = max(0, bt["price_cents"] - bt["deposit_cents"])
                conn.execute(
                    "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note,provisional)"
                    " VALUES(?,?,?,?, 'COMPENSATION',?,?,1)",
                    (f"{set_ref}-{box['id']}-SC", box["id"], customer_id, box["batch_id"],
                     comp, f"报废赔偿抵扣 {bc}（待财务复核）"),
                )
                if rcvb:
                    conn.execute(
                        "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note,provisional)"
                        " VALUES(?,?,?,?, 'RECEIVABLE',?,?,1)",
                        (f"{set_ref}-{box['id']}-SCR", box["id"], customer_id, box["batch_id"],
                         rcvb, f"报废赔偿超出押金应收 {bc}（待确认）"),
                    )
                line.update(deduct_cents=comp, receivable_cents=rcvb, provisional=True)
            accepted.append(line)

        payload = {
            "ref": set_ref,
            "kind": "RETURN_SET",
            "customer_id": customer_id,
            "customer_name": cust["name"],
            "expected_batch_id": expected_batch_id,
            "mixed_batch": len({l["batch_code"] for l in accepted}) > 1,
            "accepted": accepted,
            "rejected": rejected,
            "totals": {
                "refund_cents": sum(l["refund_cents"] for l in accepted),
                "deduct_cents": sum(l["deduct_cents"] for l in accepted),
                "receivable_cents": sum(l["receivable_cents"] for l in accepted),
                "count": len(accepted),
            },
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        finance.submit(set_ref, "RETURN_SET", payload)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return payload


# ---- 维修池 ----------------------------------------------------------------
def repair_done(conn: sqlite3.Connection, box_id: int,
                actor_role: str = ROLE_WAREHOUSE, actual_fee_cents: int | None = None) -> dict:
    """维修完成：箱子重新入库（实物操作；费用以财务复核为准）。"""
    require_role("repair_done", actor_role)
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box:
        raise DomainError("NO_BOX", "箱号不存在", 404)
    if box["state"] != "REPAIR":
        raise DomainError("BAD_STATE", f"箱子当前状态 {box['state']}，不在维修池", 409)
    conn.execute("BEGIN IMMEDIATE")
    try:
        detail = {"actual_fee_cents": actual_fee_cents}
        conn.execute(
            "UPDATE boxes SET state='IN_STOCK',customer_id=NULL,batch_id=NULL,"
            "disposition=NULL,returned_at=NULL,note=NULL WHERE id=? AND state='REPAIR'",
            (box_id,),
        )
        conn.execute(
            "INSERT INTO box_events(box_id,box_code,customer_id,batch_id,event,actor_role,detail)"
            " VALUES(?,?,?,?,'REPAIR_DONE',?,?)",
            (box_id, box["code"], box["customer_id"], box["batch_id"], actor_role,
             json.dumps(detail, ensure_ascii=False)),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"box_code": box["code"], "state": "IN_STOCK"}


# ---- 财务复核 ---------------------------------------------------------------
def _pending_rows(conn, box_id):
    return conn.execute(
        "SELECT * FROM deposit_ledger WHERE box_id=? AND provisional=1 ORDER BY id",
        (box_id,),
    ).fetchall()


def finance_review(
    conn: sqlite3.Connection, finance: MockFinanceAdapter, box_id: int,
    action: str, actor_role: str = ROLE_FINANCE,
    repair_fee_cents: int | None = None, comp_cents: int | None = None,
    note: str = "",
) -> dict:
    """财务复核维修/报废折抵：CONFIRM 确认（可改金额）或 WAIVE 豁免。"""
    require_role("review", actor_role)
    if action not in ("CONFIRM", "WAIVE"):
        raise DomainError("BAD_ACTION", "复核动作只能是 CONFIRM / WAIVE")
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box:
        raise DomainError("NO_BOX", "箱号不存在", 404)
    pending = _pending_rows(conn, box_id)
    if not pending:
        raise DomainError("NOTHING_PENDING", "该箱没有待复核的折抵", 409)
    bt = conn.execute("SELECT * FROM box_types WHERE code=?",
                      (box["type_code"],)).fetchone()

    conn.execute("BEGIN IMMEDIATE")
    try:
        finalized = []
        for r in pending:
            if action == "CONFIRM":
                amount = r["amount_cents"]
                if r["kind"] == "REPAIR_DEDUCT" and repair_fee_cents is not None:
                    amount = min(bt["deposit_cents"], repair_fee_cents)
                if r["kind"] == "REPAIR_RECEIVABLE" and repair_fee_cents is not None:
                    amount = max(0, repair_fee_cents - bt["deposit_cents"])
                if r["kind"] == "COMPENSATION" and comp_cents is not None:
                    amount = min(bt["deposit_cents"], comp_cents)
                if r["kind"] == "RECEIVABLE" and comp_cents is not None:
                    amount = max(0, comp_cents - bt["deposit_cents"])
                conn.execute(
                    "UPDATE deposit_ledger SET amount_cents=?,provisional=0,"
                    "note=COALESCE(note,'')||? WHERE id=?",
                    (amount, "｜财务已确认", r["id"]),
                )
                finalized.append({"ref": r["ref"], "kind": r["kind"],
                                  "amount_cents": amount})
            else:  # WAIVE：待复核折抵本就未计入余额，直接标记豁免并从汇总剔除
                conn.execute(
                    "UPDATE deposit_ledger SET provisional=0,waived=1,"
                    "note=COALESCE(note,'')||? WHERE id=?",
                    (f"｜已豁免（{note or '财务复核'}）", r["id"]),
                )
                finalized.append({"ref": r["ref"], "kind": r["kind"],
                                  "amount_cents": r["amount_cents"], "waived": True})
        ref = _new_ref("RV")
        detail = {"action": action, "receipt": ref,
                  "repair_fee_cents": repair_fee_cents, "comp_cents": comp_cents,
                  "note": note}
        conn.execute(
            "INSERT INTO box_events(box_id,box_code,customer_id,batch_id,event,actor_role,detail)"
            " VALUES(?,?,?,?,'FINANCE_REVIEW',?,?)",
            (box_id, box["code"], box["customer_id"], box["batch_id"], actor_role,
             json.dumps(detail, ensure_ascii=False)),
        )
        payload = {"ref": ref, "box_code": box["code"], "action": action,
                   "finalized": finalized, "note": note}
        finance.submit(ref, "REVIEW", payload)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return payload


# ---- 长期未返：核销赔偿 ------------------------------------------------------
def settle_lost(conn: sqlite3.Connection, finance: MockFinanceAdapter, box_id: int,
                actor_role: str = ROLE_FINANCE, note: str = "") -> dict:
    """长期未返箱子按丢失核销：押金抵赔偿，不足转应收。"""
    require_role("settle_lost", actor_role)
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box:
        raise DomainError("NO_BOX", "箱号不存在", 404)
    if box["state"] != "OUT":
        raise DomainError("BAD_STATE", f"箱子当前状态 {box['state']}，无法核销", 409)
    bt = conn.execute("SELECT * FROM box_types WHERE code=?",
                      (box["type_code"],)).fetchone()
    comp = min(bt["deposit_cents"], bt["price_cents"])
    rcvb = max(0, bt["price_cents"] - bt["deposit_cents"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE boxes SET state='LOST',disposition='LOST',note=? "
            "WHERE id=? AND state='OUT'", (note or "长期未返核销", box_id))
        if cur.rowcount == 0:
            raise DomainError("CONFLICT", "并发冲突，箱子状态已变化", 409)
        conn.execute(
            "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note)"
            " VALUES(?,?,?,?, 'COMPENSATION',?,?)",
            (_new_ref("LS"), box_id, box["customer_id"], box["batch_id"], comp,
             f"长期未返核销赔偿 {box['code']}"),
        )
        if rcvb:
            conn.execute(
                "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note)"
                " VALUES(?,?,?,?, 'RECEIVABLE',?,?)",
                (_new_ref("LSR"), box_id, box["customer_id"], box["batch_id"], rcvb,
                 f"丢失赔偿不足押金应收 {box['code']}"),
            )
        conn.execute(
            "INSERT INTO box_events(box_id,box_code,customer_id,batch_id,event,actor_role,detail)"
            " VALUES(?,?,?,?, 'SETTLE_LOST',?,?)",
            (box_id, box["code"], box["customer_id"], box["batch_id"], actor_role,
             json.dumps({"comp_cents": comp, "receivable_cents": rcvb, "note": note},
                        ensure_ascii=False)),
        )
        ref = _new_ref("ST")
        payload = {"ref": ref, "box_code": box["code"], "comp_cents": comp,
                   "receivable_cents": rcvb}
        finance.submit(ref, "SETTLE", payload)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return payload


# ---- 押金争议 ---------------------------------------------------------------
def open_dispute(conn: sqlite3.Connection, box_id: int, claim_amount_cents: int,
                 actor_role: str = ROLE_FINANCE, note: str = "") -> dict:
    require_role("dispute_open", actor_role)
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    if not box or not box["customer_id"]:
        raise DomainError("NO_BOX", "箱号不存在或未发放给客户", 404)
    cur = conn.execute(
        "INSERT INTO deposit_disputes(box_id,customer_id,claim_amount_cents,note)"
        " VALUES(?,?,?,?)",
        (box_id, box["customer_id"], claim_amount_cents, note),
    )
    conn.execute(
        "INSERT INTO box_events(box_id,box_code,customer_id,batch_id,event,actor_role,detail)"
        " VALUES(?,?,?,?,'DISPUTE_OPEN',?,?)",
        (box_id, box["code"], box["customer_id"], box["batch_id"], actor_role,
         json.dumps({"claim_cents": claim_amount_cents, "note": note},
                    ensure_ascii=False)),
    )
    return {"dispute_id": cur.lastrowid, "box_code": box["code"]}


def resolve_dispute(conn: sqlite3.Connection, finance: MockFinanceAdapter,
                    dispute_id: int, resolution: str,
                    approved_refund_cents: int = 0,
                    actor_role: str = ROLE_FINANCE) -> dict:
    require_role("dispute_resolve", actor_role)
    d = conn.execute("SELECT * FROM deposit_disputes WHERE id=?", (dispute_id,)).fetchone()
    if not d:
        raise DomainError("NO_DISPUTE", "争议单不存在", 404)
    if d["status"] != "OPEN":
        raise DomainError("CLOSED", "争议已处理", 409)
    box = conn.execute("SELECT * FROM boxes WHERE id=?", (d["box_id"],)).fetchone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if approved_refund_cents > 0:
            conn.execute(
                "INSERT INTO deposit_ledger(ref,box_id,customer_id,batch_id,kind,amount_cents,note)"
                " VALUES(?,?,?,?, 'REFUND',?,?)",
                (_new_ref("DR"), box["id"], d["customer_id"], box["batch_id"],
                 approved_refund_cents, f"争议补退 {box['code']}：{resolution}"),
            )
        conn.execute(
            "UPDATE deposit_disputes SET status='RESOLVED',resolution=?,"
            "resolved_at=datetime('now') WHERE id=?",
            (f"{resolution}（补退 {approved_refund_cents / 100:.2f} 元）", dispute_id),
        )
        conn.execute(
            "INSERT INTO box_events(box_id,box_code,customer_id,batch_id,event,actor_role,detail)"
            " VALUES(?,?,?,?,'DISPUTE_RESOLVE',?,?)",
            (box["id"], box["code"], d["customer_id"], box["batch_id"], actor_role,
             json.dumps({"resolution": resolution,
                         "approved_refund_cents": approved_refund_cents},
                        ensure_ascii=False)),
        )
        ref = _new_ref("DP")
        payload = {"ref": ref, "dispute_id": dispute_id, "box_code": box["code"],
                   "resolution": resolution,
                   "approved_refund_cents": approved_refund_cents}
        finance.submit(ref, "DISPUTE", payload)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return payload


# ---- 查询 ------------------------------------------------------------------
def customer_summary(conn: sqlite3.Connection) -> list[dict]:
    """箱账按客户汇总，可据此展开到箱号。"""
    out = []
    for c in conn.execute("SELECT * FROM customers ORDER BY code"):
        cid = c["id"]
        counts = {r["event"]: r["n"] for r in conn.execute(
            "SELECT event, COUNT(*) n FROM box_events WHERE customer_id=? "
            "AND event IN ('ISSUE','RETURN_OK','RETURN_REPAIR','RETURN_SCRAP','SETTLE_LOST')"
            " GROUP BY event", (cid,))}
        st = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) n FROM boxes WHERE customer_id=? GROUP BY state", (cid,))}
        led = _ledger_totals(conn, cid)
        out.append({
            "id": cid, "code": c["code"], "name": c["name"],
            "issued": counts.get("ISSUE", 0),
            "returned_ok": counts.get("RETURN_OK", 0),
            "in_repair": st.get("REPAIR", 0),
            "scrapped": counts.get("RETURN_SCRAP", 0),
            "lost": counts.get("SETTLE_LOST", 0),
            "out": st.get("OUT", 0),
            **led,
        })
    return out


def _ledger_totals(conn: sqlite3.Connection, customer_id: int) -> dict:
    rows = conn.execute(
        "SELECT kind, provisional, SUM(amount_cents) s, COUNT(*) n "
        "FROM deposit_ledger WHERE customer_id=? AND COALESCE(waived,0)=0 "
        "GROUP BY kind, provisional",
        (customer_id,)).fetchall()
    charged = refunded = deducted = 0
    pending_deduct = receivable = pending_receivable = 0
    for r in rows:
        amt = r["s"]
        if r["kind"] == "CHARGE":
            charged += amt
        elif r["kind"] == "REFUND":
            refunded += amt
        elif r["kind"] in ("REPAIR_DEDUCT", "COMPENSATION"):
            if r["provisional"]:
                pending_deduct += amt
            else:
                deducted += amt
        elif r["kind"] in ("RECEIVABLE", "REPAIR_RECEIVABLE"):
            if r["provisional"]:
                pending_receivable += amt
            else:
                receivable += amt
        elif r["kind"] == "ADJUST":
            deducted += amt  # 带符号：豁免为负
        elif r["kind"] == "RECEIVABLE_REVERSE":
            receivable -= amt
    balance = charged - refunded - deducted
    return {
        "deposit_charged_cents": charged,
        "refunded_cents": refunded,
        "deducted_cents": deducted,
        "pending_deduct_cents": pending_deduct,
        "receivable_cents": max(0, receivable),
        "pending_receivable_cents": pending_receivable,
        "balance_cents": balance,
    }


def customer_boxes(conn: sqlite3.Connection, customer_id: int,
                   state: str | None = None) -> list[dict]:
    sql = ("SELECT b.id,b.code,b.type_code,b.state,b.disposition,b.due_at,b.out_at,"
           "b.returned_at,b.photo_path,b.note,ob.code batch_code "
           "FROM boxes b LEFT JOIN outbound_batches ob ON ob.id=b.batch_id "
           "WHERE b.customer_id=?")
    args: list = [customer_id]
    if state:
        sql += " AND b.state=?"
        args.append(state)
    sql += " ORDER BY b.id"
    return [dict(r) for r in conn.execute(sql, args)]


def box_events(conn: sqlite3.Connection, box_code: str) -> list[dict]:
    rows = conn.execute(
        "SELECT e.event,e.actor_role,e.detail,e.created_at "
        "FROM box_events e JOIN boxes b ON b.id=e.box_id WHERE b.code=? ORDER BY e.id",
        (box_code,)).fetchall()
    return [{"event": r["event"], "actor_role": r["actor_role"],
             "detail": json.loads(r["detail"] or "{}"), "created_at": r["created_at"]}
            for r in rows]


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT ob.id,ob.code,c.name customer_name,ob.type_code,ob.qty,ob.due_at,
                  ob.created_at,
                  SUM(CASE WHEN b.state='OUT' THEN 1 ELSE 0 END) out_qty,
                  SUM(CASE WHEN b.state='REPAIR' THEN 1 ELSE 0 END) repair_qty,
                  SUM(CASE WHEN b.state='SCRAPPED' THEN 1 ELSE 0 END) scrap_qty,
                  SUM(CASE WHEN b.state='LOST' THEN 1 ELSE 0 END) lost_qty,
                  SUM(CASE WHEN b.state='IN_STOCK' THEN 1 ELSE 0 END) in_qty
           FROM outbound_batches ob
           JOIN customers c ON c.id=ob.customer_id
           LEFT JOIN boxes b ON b.batch_id=ob.id
           GROUP BY ob.id ORDER BY ob.id""")]


def open_batches(conn: sqlite3.Connection, customer_id: int) -> list[dict]:
    """客户仍有未结箱号的出库批次（返箱页批次选择）。"""
    return [dict(r) for r in conn.execute(
        """SELECT ob.id,ob.code,ob.type_code,ob.due_at,COUNT(b.id) open_qty
           FROM outbound_batches ob JOIN boxes b
             ON b.batch_id=ob.id AND b.customer_id=? AND b.state='OUT'
           WHERE ob.customer_id=?
           GROUP BY ob.id ORDER BY ob.id""", (customer_id, customer_id))]


def return_context(conn: sqlite3.Connection, customer_id: int) -> dict:
    """返箱页同屏数据：应返 / 实返 / 待修 + 逾期。"""
    counts = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) n FROM boxes WHERE customer_id=? GROUP BY state",
        (customer_id,))}
    ev = {r["event"]: r["n"] for r in conn.execute(
        "SELECT event, COUNT(*) n FROM box_events WHERE customer_id=? "
        "AND event IN ('ISSUE','RETURN_OK') GROUP BY event", (customer_id,))}
    overdue = conn.execute(
        "SELECT COUNT(*) n FROM boxes WHERE customer_id=? AND state='OUT' "
        "AND due_at < date('now')", (customer_id,)).fetchone()["n"]
    return {
        "should_return": counts.get("OUT", 0),       # 应返（在途未回）
        "returned_ok": ev.get("RETURN_OK", 0),       # 实返（合格已入库）
        "in_repair": counts.get("REPAIR", 0),        # 待修
        "scrapped": counts.get("SCRAPPED", 0),
        "lost": counts.get("LOST", 0),
        "overdue": overdue,
        "issued": ev.get("ISSUE", 0),
        "open_batches": open_batches(conn, customer_id),
        "out_boxes": customer_boxes(conn, customer_id, "OUT"),
    }


def repair_pool(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT b.id,b.code,b.type_code,c.name customer_name,ob.code batch_code,
                  b.returned_at,b.photo_path,b.note,
                  (SELECT amount_cents FROM deposit_ledger l WHERE l.box_id=b.id
                     AND l.kind='REPAIR_DEDUCT' ORDER BY l.id DESC LIMIT 1) deduct_cents,
                  (SELECT provisional FROM deposit_ledger l WHERE l.box_id=b.id
                     AND l.kind='REPAIR_DEDUCT' ORDER BY l.id DESC LIMIT 1) provisional
           FROM boxes b JOIN customers c ON c.id=b.customer_id
           LEFT JOIN outbound_batches ob ON ob.id=b.batch_id
           WHERE b.state='REPAIR' ORDER BY b.returned_at""").fetchall()
    return [dict(r) for r in rows]


def overdue_list(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT b.id,b.code,b.type_code,c.name customer_name,ob.code batch_code,
                  b.due_at, CAST(julianday('now')-julianday(b.due_at) AS INTEGER) days_overdue,
                  bt.price_cents
           FROM boxes b JOIN customers c ON c.id=b.customer_id
           JOIN outbound_batches ob ON ob.id=b.batch_id
           JOIN box_types bt ON bt.code=b.type_code
           WHERE b.state='OUT' AND b.due_at < date('now')
           ORDER BY b.due_at""").fetchall()
    return [dict(r) for r in rows]


def deposit_flow(conn: sqlite3.Connection, customer_id: int | None = None) -> list[dict]:
    sql = ("SELECT l.id,l.ref,l.kind,l.amount_cents,l.provisional,l.waived,l.note,"
           "l.receipt_ref,l.created_at,b.code box_code,c.name customer_name,"
           "ob.code batch_code "
           "FROM deposit_ledger l JOIN boxes b ON b.id=l.box_id "
           "JOIN customers c ON c.id=l.customer_id "
           "LEFT JOIN outbound_batches ob ON ob.id=l.batch_id")
    args: list = []
    if customer_id:
        sql += " WHERE l.customer_id=?"
        args.append(customer_id)
    sql += " ORDER BY l.id DESC LIMIT 500"
    return [dict(r) for r in conn.execute(sql, args)]


def list_disputes(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT d.*, b.code box_code, c.name customer_name
           FROM deposit_disputes d JOIN boxes b ON b.id=d.box_id
           JOIN customers c ON c.id=d.customer_id ORDER BY d.id DESC""")]
