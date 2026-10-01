"""运行期配置：全部落在仓库本地目录，无外部依赖。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TOTEFLOW_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "toteflow.db"
PHOTO_DIR = DATA_DIR / "photos"

# 押金与逾期规则（元 / 天）
DEPOSIT_BY_TYPE = {
    "标准箱-A": 120.0,
    "高箱-B": 180.0,
    "折叠箱-C": 90.0,
}
REPAIR_FEE_CAP = 80.0            # 单次维修费用封顶
OVERDUE_DAYS = 30                # 出库超过 N 天未返还即逾期
DISPUTE_HOLD_NOTE = "押金争议：冻结返还，待财务复核"

# 演示账号 —— 用户名即角色（真实系统应换为哈希口令 + 用户表）
ACCOUNTS = {
    "wh": ("wh123", "warehouse", "仓库-王建国"),
    "fin": ("fin123", "finance", "财务-李晓芸"),
    "admin": ("admin123", "admin", "主管-陈静"),
}
ROLE_NAMES = {"warehouse": "仓库", "finance": "财务", "admin": "主管"}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
