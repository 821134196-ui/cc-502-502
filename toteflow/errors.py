"""领域错误：HTTP 层据此给出 4xx 与中文提示。"""


class ToteError(Exception):
    status = 400


class NotFound(ToteError):
    status = 404


class Conflict(ToteError):
    """状态冲突：重复退箱、箱子不在外、已在维修等。"""
    status = 409


class Forbidden(ToteError):
    status = 403


class OwnerMismatch(ToteError):
    """箱号不属于该客户。"""
    status = 422


class CrossBatchReturn(ToteError):
    """跨批次返还：需主管确认后才能入账。"""
    status = 422


class DepositDispute(ToteError):
    """该箱存在未决押金争议，冻结抵扣。"""
    status = 423
