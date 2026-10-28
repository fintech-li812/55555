# -*- coding: utf-8 -*-
"""统一错误码与异常类型。

约定：所有对外接口返回 {"ok": false, "code": <int>, "error": <str>, "detail": ...}
状态码分区：
  0     成功
  10xx  配置/启动问题
  20xx  数据源问题
  30xx  业务执行问题
  40xx  鉴权与二次确认
  50xx  未预期错误
"""
from __future__ import annotations

OK = 0

ERR_CONFIG = 1001        # 配置缺失或非法
ERR_CONFIG_SECRET = 1002  # 敏感配置缺失

ERR_DB_CONNECT = 2001
ERR_DB_QUERY = 2002
ERR_DB_WRITE = 2003
ERR_DB_DENIED = 2004     # 写操作超出最小权限（只允 INSERT）

ERR_NO_DATA = 3001       # 数据源无可用数据（如实返回，不编造）
ERR_JOB_FAILED = 3002
ERR_CHANNEL = 3003

ERR_AUTH = 4001
ERR_NEED_CONFIRM = 4002  # 高风险操作需要二次确认
ERR_BAD_REQUEST = 4003
ERR_NOT_FOUND = 4004

ERR_INTERNAL = 5001


class AppError(Exception):
    """可预期的业务/运行时错误，带稳定错误码。"""

    def __init__(self, code: int, message: str, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self):
        d = {"ok": False, "code": self.code, "error": self.message}
        if self.detail is not None:
            d["detail"] = self.detail
        return d
