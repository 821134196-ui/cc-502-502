"""播种演示数据：箱型、客户、四个出库批次，以及一次“混批返箱”样例。

混批样例（客户 华东生鲜 C01 一次扫码）：
  R60-00006 合格入库（批次 B20260901）
  R60-00007 破损→维修池（批次 B20260901，折抵待财务复核）
  R80-00001 合格入库（批次 B20260915）
  R80-00002 报废（批次 B20260915，赔偿折抵待复核）
  R60-00009 → 拒收：箱号属于北辰物流（NOT_OWNED）
  R60-00006 → 拒收：重复退箱（ALREADY_RETURNED）

默认仅在数据库为空时播种；--reset 会清空重建。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .db import connect, init_db
from .finance import MockFinanceAdapter
from . import services as svc
from .server import DB_PATH, PHOTOS_DIR, RECEIPTS_DIR

DATA = Path(__file__).resolve().parent.parent / "data"

TYPES = [
    # code, 名称, 押金, 标准维修费, 采购价（单位：分）
    ("R60", "60L 标准周转箱", 6000, 3000, 9000),
    ("R80", "80L 带盖周转箱", 10000, 5000, 16000),
    ("L120", "120L 大型料箱", 18000, 12000, 30000),
]

PHOTO_SVG = """<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200'>
<rect width='320' height='200' fill='#e8ecf0'/>
<rect x='14' y='14' width='292' height='172' fill='none' stroke='#3a424d' stroke-width='3'/>
<text x='160' y='92' font-family='sans-serif' font-size='26' fill='#3a424d'
 text-anchor='middle'>{code}</text>
<text x='160' y='128' font-family='sans-serif' font-size='16' fill='#b4453a'
 text-anchor='middle'>{mark}</text>
<path d='M40 150 L120 60 L200 150' fill='none' stroke='#b4453a' stroke-width='3'/>
</svg>"""


def _photo(code: str, mark: str) -> str:
    path = PHOTOS_DIR / f"{code}_seed.svg"
    path.write_text(PHOTO_SVG.format(code=code, mark=mark), encoding="utf-8")
    return f"photos/{path.name}"


def seed(reset: bool = False) -> None:
    DATA.mkdir(exist_ok=True)
    if reset:
        for pat in ("turnover.db", "turnover.db-wal", "turnover.db-shm"):
            p = DATA / pat
            if p.exists():
                p.unlink()
        import shutil
        if RECEIPTS_DIR.exists():
            shutil.rmtree(RECEIPTS_DIR)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect(DB_PATH)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) n FROM customers").fetchone()["n"] and not reset:
        print("数据库已有数据，跳过播种（如需重建：python -m app.seed --reset）")
        conn.close()
        return
    fin = MockFinanceAdapter(conn, RECEIPTS_DIR)

    for code, name, dep, rep, price in TYPES:
        svc.create_box_type(conn, code, name, dep, rep, price)
    c1 = svc.create_customer(conn, "C01", "华东生鲜连锁")
    c2 = svc.create_customer(conn, "C02", "北辰物流")
    c3 = svc.create_customer(conn, "C03", "广南电子")

    # 四个出库批次（前两个已逾期）
    svc.issue_boxes(conn, c1, "R60", 8, "2026-09-20",
                    svc.ROLE_WAREHOUSE, "B20260901")
    svc.issue_boxes(conn, c1, "R80", 5, "2026-10-15",
                    svc.ROLE_WAREHOUSE, "B20260915")
    svc.issue_boxes(conn, c2, "R60", 6, "2026-09-28",
                    svc.ROLE_WAREHOUSE, "B20260918")
    svc.issue_boxes(conn, c3, "L120", 4, "2026-10-20",
                    svc.ROLE_WAREHOUSE, "B20260925")

    # 历史已闭环验收：3 合格 + 1 维修（财务已确认 25 元）+ 1 报废（财务已确认）
    earlier = [
        {"box_code": "R60-00001", "verdict": "OK"},
        {"box_code": "R60-00002", "verdict": "OK"},
        {"box_code": "R60-00003", "verdict": "OK",
         "body_ok": True, "lid_ok": True, "label_ok": True},
        {"box_code": "R60-00004", "verdict": "REPAIR",
         "body_ok": False, "lid_ok": True, "label_ok": True,
         "photo_path": _photo("R60-00004", "箱体裂纹"), "note": "箱体裂纹"},
        {"box_code": "R60-00005", "verdict": "SCRAP",
         "body_ok": False, "lid_ok": False, "label_ok": False,
         "photo_path": _photo("R60-00005", "严重变形报废"), "note": "挤压严重变形"},
    ]
    r1 = svc.return_boxes(conn, fin, c1, earlier, svc.ROLE_WAREHOUSE,
                          expected_batch_id=1, set_ref="RC-SEED-EARLY")
    # 维修完成回库
    b4 = conn.execute("SELECT id FROM boxes WHERE code='R60-00004'").fetchone()
    svc.finance_review(conn, fin, b4["id"], "CONFIRM", svc.ROLE_FINANCE,
                       repair_fee_cents=2500, note="按维修工单确认 25 元")
    svc.repair_done(conn, b4["id"], svc.ROLE_WAREHOUSE)
    b5 = conn.execute("SELECT id FROM boxes WHERE code='R60-00005'").fetchone()
    svc.finance_review(conn, fin, b5["id"], "CONFIRM", svc.ROLE_FINANCE,
                       comp_cents=9000, note="按采购价赔偿")

    # 混批返箱：不指定 expected_batch_id，两个批次同屏验收
    mixed = [
        {"box_code": "R60-00006", "verdict": "OK"},
        {"box_code": "R60-00007", "verdict": "REPAIR",
         "body_ok": False, "lid_ok": True, "label_ok": True,
         "photo_path": _photo("R60-00007", "箱体破洞-待修"),
         "note": "侧壁破洞 8cm"},
        {"box_code": "R80-00001", "verdict": "OK"},
        {"box_code": "R80-00002", "verdict": "SCRAP",
         "body_ok": False, "lid_ok": False, "label_ok": False,
         "photo_path": _photo("R80-00002", "封盖碎裂-报废"),
         "note": "封盖碎裂无法修复"},
        # 异常样例：非本客户箱号、重复退箱
        {"box_code": "R60-00009", "verdict": "OK"},
        {"box_code": "R60-00006", "verdict": "OK"},
    ]
    r2 = svc.return_boxes(conn, fin, c1, mixed, svc.ROLE_WAREHOUSE,
                          expected_batch_id=None, set_ref="RC-SEED-MIXED")

    # 押金争议样例：客户主张仍在途的 R60-00008 押金重复收取
    b8 = conn.execute("SELECT id FROM boxes WHERE code='R60-00008'").fetchone()
    svc.open_dispute(conn, b8["id"], 6000, svc.ROLE_FINANCE,
                     "客户称该箱押金已于上月货款中重复收取，申请核实")

    conn.close()
    print("播种完成：")
    print(f"  出库批次 4 个；早期验收 {len(r1['accepted'])} 箱（已闭环）")
    print(f"  混批返箱受理 {r2['totals']['count']} 箱、拒收 {len(r2['rejected'])} 箱"
          f"（{[x['reason'] for x in r2['rejected']]}）")
    print("  待财务复核：R60-00007 维修折抵、R80-00002 报废折抵")
    print("  逾期未返 7 箱；未决押金争议 1 单")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空数据库后重建")
    a = ap.parse_args()
    seed(a.reset)
