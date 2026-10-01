"""只读查询：客户箱账、返箱看板（应返/实返/待修）、维修池、逾期、押金流水。"""
from __future__ import annotations

from datetime import datetime

from . import config

STATE_CN = {"out": "在外", "stock": "在库", "repair": "待修",
            "scrapped": "已报废", "lost": "已核销"}


def customers(conn):
    return [dict(r) for r in conn.execute(
        "SELECT cid,name,contact FROM customers ORDER BY cid")]


def batches(conn):
    return [dict(r) for r in conn.execute(
        "SELECT bid,note,created_at FROM batches ORDER BY bid")]


def box_detail(conn, box_no: str):
    row = conn.execute("SELECT * FROM boxes WHERE box_no=?", (box_no,)).fetchone()
    return dict(row) if row else None


def box_events(conn, box_no: str):
    return [dict(r) for r in conn.execute(
        "SELECT id,event,actor,at,payload FROM box_events "
        "WHERE box_no=? ORDER BY id", (box_no,))]


def customer_account(conn, cid: str):
    """客户箱账：按客户汇总（应返/实返/待修/报废、押金收/退/扣/余），可展开到箱号。"""
    cust = conn.execute("SELECT * FROM customers WHERE cid=?", (cid,)).fetchone()
    if not cust:
        return None
    boxes = [dict(r) for r in conn.execute(
        """SELECT b.*,
                  CASE WHEN r.decision IS NOT NULL THEN r.decision END AS last_decision,
                  r.reason AS return_reason, r.repair_fee AS last_fee
             FROM boxes b
             LEFT JOIN (SELECT box_no, decision, reason, repair_fee,
                               ROW_NUMBER() OVER (PARTITION BY box_no ORDER BY id DESC) rn
                        FROM returns) r ON r.box_no=b.box_no AND r.rn=1
            WHERE b.holder_cid=?
               OR b.box_no IN (SELECT box_no FROM returns WHERE actual_cid=?)
               OR b.box_no IN (SELECT box_no FROM deposit_ledger WHERE cid=?)
            ORDER BY b.dispatched_at DESC, b.box_no""",
        (cid, cid, cid))]

    out = sum(1 for b in boxes if b["state"] == "out")
    repair = sum(1 for b in boxes if b["state"] == "repair")
    scrapped = sum(1 for b in boxes if b["state"] in ("scrapped", "lost"))
    returned = sum(1 for b in boxes if b["state"] == "stock")

    led = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN status='confirmed' AND tx_type='CHARGE'
                         THEN amount END),0)  AS charged,
                  COALESCE(SUM(CASE WHEN status='confirmed' AND tx_type='REFUND'
                         THEN amount END),0)  AS refunded,
                  COALESCE(SUM(CASE WHEN status='confirmed'
                         AND tx_type LIKE 'DEDUCT%' THEN amount END),0) AS deducted,
                  COALESCE(SUM(CASE WHEN status='pending' THEN amount
                         * CASE WHEN tx_type='CHARGE' THEN 1 ELSE -1 END END),0)
                         AS pending_net
             FROM deposit_ledger WHERE cid=?""", (cid,)).fetchone()
    charged = led["charged"]
    settled = led["refunded"] + led["deducted"]
    return {
        "customer": dict(cust),
        "boxes": boxes,
        "summary": {
            "total": len(boxes), "out": out, "returned": returned,
            "repair": repair, "scrapped": scrapped,
            "charged": round(charged, 2),
            "refunded": round(led["refunded"], 2),
            "deducted": round(led["deducted"], 2),
            "balance": round(charged - settled, 2),
            "pending_net": round(led["pending_net"], 2),
        },
    }


def all_accounts(conn):
    """以发箱 CHARGE 流水为全集统计（箱子回库后 holder_cid 会清空）。"""
    dispatched: dict[str, dict] = {}
    for r in conn.execute(
            """SELECT b.state, l.cid
                 FROM boxes b JOIN deposit_ledger l
                   ON l.box_no=b.box_no AND l.tx_type='CHARGE'"""):
        d = dispatched.setdefault(r["cid"], {"total": 0, "out": 0, "stock": 0,
                                             "repair": 0, "scrap": 0})
        d["total"] += 1
        d[{"out": "out", "stock": "stock", "repair": "repair"}.get(
            r["state"], "scrap")] += 1

    result = []
    for c in conn.execute("SELECT cid,name FROM customers ORDER BY cid"):
        d = dispatched.get(c["cid"], {"total": 0, "out": 0, "stock": 0,
                                      "repair": 0, "scrap": 0})
        led = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='confirmed'
                              AND tx_type='CHARGE' THEN amount END),0) ch,
                      COALESCE(SUM(CASE WHEN status='confirmed'
                              AND tx_type<>'CHARGE' THEN amount END),0) used
                 FROM deposit_ledger WHERE cid=?""", (c["cid"],)).fetchone()
        result.append({
            "cid": c["cid"], "name": c["name"], **d,
            "balance": round(led["ch"] - led["used"], 2),
        })
    return result


def return_board(conn):
    """返箱页同屏数据：按客户列出 应返 / 实返(在库) / 待修。"""
    board = {}
    for r in conn.execute(
            """SELECT l.cid,
                      SUM(CASE WHEN b.state='out' THEN 1 ELSE 0 END) AS due,
                      SUM(CASE WHEN b.state='stock' THEN 1 ELSE 0 END) AS back,
                      SUM(CASE WHEN b.state='repair' THEN 1 ELSE 0 END) AS repair,
                      SUM(CASE WHEN b.state IN ('scrapped','lost') THEN 1 ELSE 0 END)
                          AS scrap
                 FROM boxes b
                 JOIN deposit_ledger l ON l.box_no=b.box_no
                WHERE l.tx_type='CHARGE'
                GROUP BY l.cid"""):
        board[r["cid"]] = {"due": r["due"], "back": r["back"],
                           "repair": r["repair"], "scrap": r["scrap"]}
    out = []
    for c in conn.execute("SELECT cid,name FROM customers ORDER BY cid"):
        d = board.get(c["cid"], {"due": 0, "back": 0, "repair": 0, "scrap": 0})
        out.append({"cid": c["cid"], "name": c["name"], **d})
    return out


def out_boxes(conn, cid: str | None = None, batch_id: str | None = None):
    sql = ("SELECT b.*, c.name AS holder_name FROM boxes b "
           "LEFT JOIN customers c ON c.cid=b.holder_cid WHERE b.state='out'")
    args = []
    if cid:
        sql += " AND b.holder_cid=?"
        args.append(cid)
    if batch_id:
        sql += " AND b.batch_id=?"
        args.append(batch_id)
    sql += " ORDER BY b.due_at"
    return [dict(r) for r in conn.execute(sql, args)]


def overdue_boxes(conn, days: int = config.OVERDUE_DAYS):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """SELECT b.*, c.name AS holder_name,
                  CAST(julianday(?) - julianday(b.due_at) AS INT) AS overdue_days
             FROM boxes b LEFT JOIN customers c ON c.cid=b.holder_cid
            WHERE b.state='out' AND b.due_at < ?
            ORDER BY b.due_at""", (now, now)).fetchall()
    return [dict(r) for r in rows]


def repair_pool(conn, include_closed: bool = False):
    sql = ("SELECT r.*, b.box_type, b.deposit, c.name AS holder_name "
           "FROM repairs r JOIN boxes b ON b.box_no=r.box_no "
           "LEFT JOIN returns rt ON rt.box_no=r.box_no "
           "LEFT JOIN customers c ON c.cid=rt.actual_cid WHERE 1=1")
    if not include_closed:
        sql += " AND r.closed_at IS NULL"
    sql += " ORDER BY r.id"
    return [dict(r) for r in conn.execute(sql)]


def ledger(conn, cid: str | None = None, status: str | None = None,
           box_no: str | None = None):
    sql = ("SELECT l.* FROM deposit_ledger l WHERE 1=1")
    args = []
    if cid:
        sql += " AND l.cid=?"
        args.append(cid)
    if status:
        sql += " AND l.status=?"
        args.append(status)
    if box_no:
        sql += " AND l.box_no=?"
        args.append(box_no)
    sql += " ORDER BY l.id DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def disputes(conn, only_open=True):
    sql = "SELECT * FROM disputes"
    if only_open:
        sql += " WHERE status='open'"
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql)]
