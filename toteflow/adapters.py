"""财务回执适配器：默认模拟实现（本地生成回执号），可替换为真实银企/ERP 接口。"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol


class FinanceGateway(Protocol):
    def submit_refund(self, *, box_no: str, cid: str, amount: float,
                      ref: str) -> str: ...
    def submit_deduction(self, *, box_no: str, cid: str, amount: float,
                         ref: str) -> str: ...


class MockFinanceAdapter:
    """模拟适配器：确定性生成回执号，不落网。换实现只需同协议替换。"""

    def _receipt(self, prefix: str, box_no: str, cid: str,
                 amount: float, ref: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d")
        digest = hashlib.sha1(
            f"{prefix}|{box_no}|{cid}|{amount:.2f}|{ref}|{stamp}".encode()
        ).hexdigest()[:8].upper()
        return f"MOCK-{prefix}-{stamp}-{digest}"

    def submit_refund(self, *, box_no, cid, amount, ref):
        return self._receipt("RFD", box_no, cid, amount, ref)

    def submit_deduction(self, *, box_no, cid, amount, ref):
        return self._receipt("DED", box_no, cid, amount, ref)


gateway: FinanceGateway = MockFinanceAdapter()
