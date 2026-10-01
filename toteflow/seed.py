"""演示种子数据：一条命令即可看到混批返箱等全部场景。"""
from __future__ import annotations

import struct
import zlib

from . import config, photos, queries, services
from .db import connect, init_db, log_event, tx
from .errors import CrossBatchReturn

WH = {"name": "仓库-王建国", "role": "warehouse"}
FIN = {"name": "财务-李晓芸", "role": "finance"}
ADMIN = {"name": "主管-陈静", "role": "admin"}

CUSTOMERS = [
    ("C001", "华东连锁超市", "采购部 周敏 13800001111"),
    ("C002", "江北食品厂", "物流科 赵磊 13800002222"),
    ("C003", "南郊餐饮集团", "仓管 孙倩 13800003333"),
]
BATCHES = [("B20260901", "9月第一批发货"), ("B20260915", "9月中旬混批发货")]


def _png(color=(180, 60, 50)) -> bytes:
    """生成一张纯色 PNG 作为破损照片占位（本地文件，不外传）。"""
    w = hgt = 96
    raw = b"".join(
        b"\x00" + bytes(color) * w for _ in range(hgt))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, hgt, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _approve_by_box(conn, box_no: str, tx_type: str, note="") -> None:
    row = conn.execute(
        "SELECT id FROM deposit_ledger WHERE box_no=? AND tx_type=? "
        "AND status='pending' ORDER BY id DESC LIMIT 1",
        (box_no, tx_type)).fetchone()
    if row:
        services.review_ledger(conn, ledger_id=row["id"], approve=True,
                               actor=FIN, note=note)


def seed(force: bool = False) -> bool:
    """返回是否执行了播种（库里已有箱号时默认跳过）。"""
    config.ensure_dirs()
    conn = connect(config.DB_PATH)
    init_db(conn)
    try:
        if conn.execute("SELECT COUNT(*) n FROM boxes").fetchone()["n"] and not force:
            return False

        if force:
            for t in ("box_events", "returns", "repairs", "disputes",
                      "deposit_ledger", "boxes", "photos", "customers", "batches"):
                conn.execute(f"DELETE FROM {t}")

        for cid, name, contact in CUSTOMERS:
            services.add_customer(conn, cid, name, contact)
        for bid, note in BATCHES:
            services.add_batch(conn, bid, note)

        def disp(no, typ, batch, cid, due=30):
            services.dispatch(conn, box_no=no, box_type=typ, batch_id=batch,
                              cid=cid, actor=WH, due_days=due)

        # C001：两批次都有（混批返还的基础）
        for no in ("T-A-1001", "T-A-1002", "T-A-1003", "T-A-1004"):
            disp(no, "标准箱-A", "B20260901", "C001")
        disp("T-B-2001", "高箱-B", "B20260901", "C001")
        disp("T-A-1006", "标准箱-A", "B20260915", "C001")
        disp("T-B-2002", "高箱-B", "B20260915", "C001")
        # C002
        disp("T-A-1005", "标准箱-A", "B20260915", "C002")
        disp("T-C-3001", "折叠箱-C", "B20260915", "C002")
        disp("T-C-3002", "折叠箱-C", "B20260915", "C002")
        # 逾期样例：T-A-1004 应返日已过 5 天
        conn.execute(
            "UPDATE boxes SET due_at=datetime('now','localtime','-5 day') "
            "WHERE box_no='T-A-1004'")
        # C003 暂无业务
        return _events(conn)
    finally:
        conn.close()


def _events(conn) -> bool:
    png = _png()

    # 1) 两箱质检合格全额退（财务已复核 → 已确认回执）
    for no in ("T-A-1001", "T-A-1002"):
        services.accept_return(conn, box_no=no, cid="C001",
                               return_batch_id="B20260901",
                               body_ok=True, lid_ok=True, label_ok=True, actor=WH)
        _approve_by_box(conn, no, "REFUND", "随批结算")

    # 2) ★混批返箱样例★：9/15 批次的箱子夹在 9/01 批次里返还
    #    仓库先扫被拦下（仅记录事件），主管确认跨批次后按实际批次入账
    for no in ("T-A-1006", "T-B-2002"):
        try:
            services.accept_return(conn, box_no=no, cid="C001",
                                   return_batch_id="B20260901",
                                   body_ok=True, lid_ok=True, label_ok=True,
                                   actor=WH)
        except CrossBatchReturn:
            pass
    # 上面的异常类型从 errors 导出；显式记录一次跨批次扫描痕迹
    for no in ("T-A-1006", "T-B-2002"):
        log_event(conn, no, "CROSS_BATCH", WH["name"],
                  {"box_batch": "B20260915", "return_batch": "B20260901",
                   "note": "混批扫描拦截，转主管确认"})
    services.accept_return(conn, box_no="T-A-1006", cid="C001",
                           return_batch_id="B20260901",
                           body_ok=True, lid_ok=True, label_ok=True,
                           actor=ADMIN, allow_cross_batch=True)
    _approve_by_box(conn, "T-A-1006", "REFUND", "跨批次核对无误")
    # T-B-2002 同样混批，留一条「待审」退款流水
    services.accept_return(conn, box_no="T-B-2002", cid="C001",
                           return_batch_id="B20260901",
                           body_ok=True, lid_ok=True, label_ok=True,
                           actor=ADMIN, allow_cross_batch=True)

    # 3) 箱体破损送修（附本地破损照片），维修完成重新入库并退余额
    pid1, _ = photos.save("T-A-1003", "damage", "crack.png", png)
    services.accept_return(conn, box_no="T-A-1003", cid="C001",
                           return_batch_id="B20260901",
                           body_ok=False, lid_ok=True, label_ok=True,
                           repair_fee=30, fault="箱体裂纹 8cm", actor=WH,
                           photo_ids=[pid1])
    services.finish_repair(conn, box_no="T-A-1003", outcome="repaired",
                           actual_fee=30, actor=WH)
    _approve_by_box(conn, "T-A-1003", "DEDUCT_REPAIR")
    _approve_by_box(conn, "T-A-1003", "REFUND")

    # 4) 在修池中待处理：封盖损坏，预估费未定
    pid, _ = photos.save("T-C-3001", "damage", "lid_broken.png", png)
    services.accept_return(conn, box_no="T-C-3001", cid="C002",
                           return_batch_id="B20260915",
                           body_ok=True, lid_ok=False, label_ok=True,
                           repair_fee=45, fault="封盖卡扣断裂", actor=WH,
                           photo_ids=[pid])

    # 5) 严重破损报废：押金全额折抵（财务已复核）
    photos.save("T-B-2001", "damage", "totaled.png", _png((120, 40, 40)))
    services.accept_return(conn, box_no="T-B-2001", cid="C001",
                           return_batch_id="B20260901",
                           body_ok=False, lid_ok=False, label_ok=False,
                           scrap=True, fault="挤压变形，无修复价值", actor=WH)
    _approve_by_box(conn, "T-B-2001", "DEDUCT_LOSS", "报废鉴定单 #0921")

    # 6) 押金争议：客户对 T-A-1005 扣费有异议，冻结该箱抵扣
    services.open_dispute(conn, box_no="T-A-1005",
                          note="客户称该箱已由司机签收，待核对签收单", actor=FIN)

    # 仍在外：T-A-1004（逾期5天）、T-A-1005（争议中）、T-C-3002（正常在外）
    return True
