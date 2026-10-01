"""破损照片本地存储：只落仓库本机磁盘，不外传。"""
from __future__ import annotations

import re
from pathlib import Path

from . import config

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _photo_root() -> Path:
    config.ensure_dirs()
    return config.PHOTO_DIR


def save(box_no: str, kind: str, filename: str, data: bytes) -> tuple[int, str]:
    """保存一张照片，返回 (photo_id, 落盘文件名)。"""
    from .db import connect, init_db  # 延迟导入，便于测试改配置

    safe_name = _SAFE.sub("_", filename) or "photo.bin"
    folder = _photo_root() / _SAFE.sub("_", box_no)
    folder.mkdir(parents=True, exist_ok=True)

    conn = connect(config.DB_PATH)
    try:
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO photos(box_no, kind, filename) VALUES (?,?,?)",
            (box_no, kind, safe_name),
        )
        photo_id = cur.lastrowid
        stored = f"{photo_id}_{safe_name}"
        (folder / stored).write_bytes(data)
        conn.execute("UPDATE photos SET filename=? WHERE id=?", (stored, photo_id))
        return photo_id, stored
    finally:
        conn.close()


def path_for(box_no: str, stored_name: str) -> Path:
    return _photo_root() / _SAFE.sub("_", box_no) / stored_name


def list_for(conn, box_no: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, filename FROM photos WHERE box_no=? ORDER BY id", (box_no,)
    ).fetchall()
    return [dict(r) for r in rows]
