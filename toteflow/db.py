"""SQLite 数据层：逐箱事件账 + 押金流水，全部带审计轨迹。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    cid         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    contact     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS batches (
    bid         TEXT PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 箱子台账：一行一箱，当前状态镜像；真相以 box_events 为准
CREATE TABLE IF NOT EXISTS boxes (
    box_no      TEXT PRIMARY KEY,
    box_type    TEXT NOT NULL,
    deposit     REAL NOT NULL,
    batch_id    TEXT NOT NULL REFERENCES batches(bid),
    holder_cid  TEXT REFERENCES customers(cid),   -- 在外时的持有客户
    state       TEXT NOT NULL,                     -- out / stock / repair / scrapped
    dispatched_at TEXT,
    due_at      TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 逐箱事件流（append-only）
CREATE TABLE IF NOT EXISTS box_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no      TEXT NOT NULL,
    event       TEXT NOT NULL,   -- DISPATCH/RETURN_ACCEPT/RETURN_REJECT/REPAIR_IN/REPAIR_DONE/SCRAP/CROSS_BATCH/HOLD/RELEASE
    actor       TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_box ON box_events(box_no, id);

-- 返箱扫描/验收记录（一次扫描一行，含三项质检与处置）
CREATE TABLE IF NOT EXISTS returns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no      TEXT NOT NULL,
    expect_cid  TEXT,                 -- 扫描时选择的客户
    actual_cid  TEXT,                 -- 箱号实际归属
    batch_id    TEXT,
    scanned_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    inspector   TEXT NOT NULL,
    body_ok     INTEGER, lid_ok INTEGER, label_ok INTEGER,
    owner_ok    INTEGER NOT NULL,
    cross_batch INTEGER NOT NULL DEFAULT 0,
    decision    TEXT NOT NULL,        -- ACCEPT/REPAIR/SCRAP/REJECT
    repair_fee  REAL NOT NULL DEFAULT 0,
    reason      TEXT,
    refund_id   INTEGER,              -- 对应押金流水（同一箱号）
    deduct_id   INTEGER
);

-- 押金流水（不可变；冲正用反向新行，不改旧行）
-- amount: 客户押金池为正，退还/扣减为负
CREATE TABLE IF NOT EXISTS deposit_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no      TEXT NOT NULL,
    cid         TEXT NOT NULL,
    batch_id    TEXT,
    tx_type     TEXT NOT NULL,        -- CHARGE/REFUND/DEDUCT_REPAIR/DEDUCT_LOSS
    amount      REAL NOT NULL,
    status      TEXT NOT NULL,        -- confirmed/pending/held/rejected
    reason      TEXT,
    receipt_no  TEXT,                 -- 财务回执编号
    created_by  TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_cid ON deposit_ledger(cid, status);
CREATE INDEX IF NOT EXISTS idx_ledger_box ON deposit_ledger(box_no);

-- 维修池
CREATE TABLE IF NOT EXISTS repairs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no      TEXT NOT NULL UNIQUE,
    opened_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    opened_by   TEXT NOT NULL,
    fee         REAL NOT NULL,
    fault       TEXT,
    photo_ids   TEXT NOT NULL DEFAULT '[]',
    closed_at   TEXT,
    outcome     TEXT                  -- repaired / scrapped
);

-- 押金争议
CREATE TABLE IF NOT EXISTS disputes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no      TEXT NOT NULL,
    cid         TEXT NOT NULL,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open/resolved
    opened_by   TEXT NOT NULL,
    opened_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolved_by TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no      TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- damage / label / other
    filename    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即拿写锁的事务：并发退箱时第二个写者排队后再靠条件 UPDATE 判重。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def log_event(conn: sqlite3.Connection, box_no: str, event: str,
              actor: str, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO box_events(box_no, event, actor, payload) VALUES (?,?,?,?)",
        (box_no, event, actor, json.dumps(payload or {}, ensure_ascii=False)),
    )
