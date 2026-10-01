"""SQLite schema and connection helpers.

All monetary values are stored as INTEGER cents to avoid float rounding.
Every physical or financial move on a box is appended to box_events /
deposit_ledger, so the per-box history is fully reconstructible.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS box_types (
    code            TEXT PRIMARY KEY,          -- e.g. R60
    name            TEXT NOT NULL,
    deposit_cents   INTEGER NOT NULL,          -- 押金/箱
    repair_cents    INTEGER NOT NULL,          -- 标准维修费
    price_cents     INTEGER NOT NULL           -- 采购价(赔偿基准)
);

CREATE TABLE IF NOT EXISTS outbound_batches (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,         -- 出库批次
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    type_code    TEXT NOT NULL REFERENCES box_types(code),
    qty          INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    due_at       TEXT NOT NULL                 -- 应返期限
);

-- 逐箱台账。一个箱号在任意时刻只属于一个客户、一个出库批次。
CREATE TABLE IF NOT EXISTS boxes (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,        -- 箱号
    type_code     TEXT NOT NULL REFERENCES box_types(code),
    -- IN_STOCK 库内 | OUT 已出库 | REPAIR 维修池 | SCRAPPED 报废 | LOST 长期未返核销
    state         TEXT NOT NULL DEFAULT 'IN_STOCK',
    customer_id   INTEGER REFERENCES customers(id),
    batch_id      INTEGER REFERENCES outbound_batches(id),
    out_at        TEXT,
    due_at        TEXT,
    returned_at   TEXT,
    disposition   TEXT,                         -- OK / REPAIR / SCRAP
    photo_path    TEXT,
    note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_boxes_customer ON boxes(customer_id);
CREATE INDEX IF NOT EXISTS idx_boxes_state ON boxes(state);

-- 逐箱事件流
CREATE TABLE IF NOT EXISTS box_events (
    id          INTEGER PRIMARY KEY,
    box_id      INTEGER NOT NULL REFERENCES boxes(id),
    box_code    TEXT NOT NULL,                 -- 箱号快照
    customer_id INTEGER REFERENCES customers(id),   -- 事件发生时客户(周转后仍可追溯)
    batch_id    INTEGER REFERENCES outbound_batches(id),
    event       TEXT NOT NULL,  -- ISSUE / RETURN_OK / RETURN_REPAIR / RETURN_SCRAP
                                -- REPAIR_DONE / SCRAP / SETTLE_LOST / DISPUTE
    actor_role  TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_box ON box_events(box_id);
CREATE INDEX IF NOT EXISTS idx_events_customer ON box_events(customer_id);

-- 押金流水（按箱号挂账）。amount_cents 带符号：正=客户资金流入押金池/扣款，负=退还
CREATE TABLE IF NOT EXISTS deposit_ledger (
    id            INTEGER PRIMARY KEY,
    ref           TEXT NOT NULL UNIQUE,
    box_id        INTEGER NOT NULL REFERENCES boxes(id),
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    batch_id      INTEGER REFERENCES outbound_batches(id),
    -- CHARGE 收押金 | REFUND 退还 | REPAIR_DEDUCT 维修抵扣
    -- COMPENSATION 赔偿抵扣 | REPAIR_RECEIVABLE 应收维修款 | RECEIVABLE 应收赔偿
    -- ADJUST 财务复核调整
    kind          TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    provisional   INTEGER NOT NULL DEFAULT 0,   -- 维修抵扣待财务复核
    waived        INTEGER NOT NULL DEFAULT 0,   -- 财务复核豁免（不计入实际抵扣）
    receipt_ref   TEXT,                          -- 财务模拟适配器回执
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_box ON deposit_ledger(box_id);
CREATE INDEX IF NOT EXISTS idx_ledger_customer ON deposit_ledger(customer_id);

CREATE TABLE IF NOT EXISTS deposit_disputes (
    id                 INTEGER PRIMARY KEY,
    box_id             INTEGER NOT NULL REFERENCES boxes(id),
    customer_id        INTEGER NOT NULL REFERENCES customers(id),
    claim_amount_cents INTEGER NOT NULL,
    note               TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN / RESOLVED
    resolution         TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at        TEXT
);

CREATE TABLE IF NOT EXISTS finance_receipts (
    ref         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,           -- RETURN_SET / REVIEW / SETTLE / DISPUTE
    payload     TEXT NOT NULL,           -- JSON 快照
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 被拒绝的异常操作（非本客户箱号、未放行跨批次、重复退箱）留痕
CREATE TABLE IF NOT EXISTS exceptions (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,           -- NOT_OWNED / CROSS_BATCH / ALREADY_RETURNED / BAD_STATE
    box_code    TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    actor_role  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS code_seq (
    type_code TEXT PRIMARY KEY,
    next_no   INTEGER NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 轻量迁移：老库补齐新增列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(deposit_ledger)")}
    if "waived" not in cols:
        conn.execute("ALTER TABLE deposit_ledger ADD COLUMN waived INTEGER NOT NULL DEFAULT 0")
