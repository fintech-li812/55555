# -*- coding: utf-8 -*-
"""高风险操作二次确认。

规则：删除 / 覆盖 / 重启 / 重建 等操作必须同时满足
  1) payload["confirm"] 为 True；
  2) payload["confirm_phrase"] 严格等于操作名（如 "restart"）；
  3) 且请求头带有效令牌。
任一不满足 → 返回 4002，不执行、不留副作用，并记录一条拒绝日志。
"""
from __future__ import annotations

from typing import Any, Dict

from .errors import AppError, ERR_NEED_CONFIRM

# 操作名 → 需要输入的确认短语
RISKY_ACTIONS = {
    "restart": "restart",
    "reset_state": "reset_state",
    "purge_logs": "purge_logs",
    "rebuild_index": "rebuild_index",
}


def require_confirm(action: str, payload: Dict[str, Any], logger) -> None:
    if action not in RISKY_ACTIONS:
        raise AppError(ERR_NEED_CONFIRM, "未知的高风险操作", {"action": action})

    phrase = RISKY_ACTIONS[action]
    got_confirm = payload.get("confirm") is True
    got_phrase = payload.get("confirm_phrase")

    if not got_confirm or got_phrase != phrase:
        logger.warning("高风险操作被拒绝（二次确认未通过）：action=%s confirm=%s phrase=%s",
                       action, got_confirm, got_phrase)
        raise AppError(
            ERR_NEED_CONFIRM,
            "该操作需二次确认：请传 {\"confirm\": true, \"confirm_phrase\": \"%s\"}" % phrase,
            {"action": action, "expected_phrase": phrase},
        )
    logger.warning("高风险操作二次确认通过：action=%s", action)
