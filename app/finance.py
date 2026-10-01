"""模拟财务适配器。

真实系统会把抵扣/退款回执推给财务中台；这里把回执 JSON 写到本地文件
data/finance_receipts/<ref>.json，并在 finance_receipts 表留一份快照。
同一个 ref 重复提交只生成一份回执（幂等），方便财务复核与重试。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class MockFinanceAdapter:
    def __init__(self, conn: sqlite3.Connection, receipts_dir: str | Path):
        self.conn = conn
        self.dir = Path(receipts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def submit(self, ref: str, kind: str, payload: dict) -> str:
        """提交一笔财务回执，幂等：已存在则直接返回既有 ref。"""
        row = self.conn.execute(
            "SELECT ref FROM finance_receipts WHERE ref=?", (ref,)
        ).fetchone()
        if row:
            return ref
        body = {
            "ref": ref,
            "kind": kind,
            "status": "ACCEPTED",
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "payload": payload,
        }
        path = self.dir / f"{ref}.json"
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        self.conn.execute(
            "INSERT INTO finance_receipts(ref, kind, payload) VALUES(?,?,?)",
            (ref, kind, json.dumps(payload, ensure_ascii=False)),
        )
        return ref
