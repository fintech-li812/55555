# -*- coding: utf-8 -*-
"""层间协议：统一信封、事件与任务状态的数据格式定义。

四层之间的所有消息都遵循本文件的定义，任何一层不得自定义私有格式。
"""
from __future__ import annotations

import datetime as _dt
import uuid

# ---------------------------------------------------------------- 任务状态机
# created → planning → running → succeeded / failed / cancelled
#         ↘ awaiting_params（参数等待用户确认）
STATE = {
    "created": "已创建",
    "planning": "编排中（千问解析意图）",
    "running": "执行中（MCP 工具调用）",
    "awaiting_params": "等待参数确认",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
TERMINAL = ("succeeded", "failed", "cancelled")

# ---------------------------------------------------------------- 上行（前端 → 网关）
# {
#   "type": "user.message" | "task.command" | "params.update" | "task.cancel",
#   "session_id": "s-xxx", "request_id": "r-xxx",
#   "text": "帮我跑一遍制造业风险评估，金额30万",
#   "params": {"industry":"制造业","scenario":"risk","amount":300000,"n_per_group":20},
#   "command": "rerun" | "cancel"
# }
UP_TYPES = ("user.message", "task.command", "params.update", "task.cancel")

# ---------------------------------------------------------------- 下行（网关 → 前端，SSE）
# {
#   "type": "task.created"|"plan.ready"|"step.started"|"step.progress"|"step.done"
#          |"agent.message"|"task.state"|"task.done"|"task.failed"|"params.accepted"
#          |"heartbeat",
#   "session_id":"s-xxx","task_id":"t-xxx","ts":"2026-10-27T21:50:00.123",
#   ... 各 type 自有字段
# }
DOWN_TYPES = ("task.created", "plan.ready", "step.started", "step.progress",
              "step.done", "agent.message", "task.state", "task.done",
              "task.failed", "params.accepted", "heartbeat")


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


def event(type_: str, session_id: str = None, task_id: str = None, **kw) -> dict:
    """构造一个下行事件。"""
    e = {"type": type_, "ts": now()}
    if session_id:
        e["session_id"] = session_id
    if task_id:
        e["task_id"] = task_id
    e.update(kw)
    return e


# ---------------------------------------------------------------- 默认参数（可被前端实时覆盖）
DEFAULT_PARAMS = {
    "industry": "制造业",
    "scenario": "edge",
    "amount": 120000.0,
    "n_per_group": 20,
    "as_of": None,          # 三态评估基准日，None=取数据期间末
}
PARAM_SCHEMA = {
    "industry": {"type": "string", "enum": ["制造业", "零售业", "建筑业", "科技服务业"],
                 "label": "行业（决定 K黄/K红）"},
    "scenario": {"type": "string", "enum": ["healthy", "edge", "risk"],
                 "label": "受评对象"},
    "amount": {"type": "number", "min": 0, "label": "本次申请金额（元，用于分级审批）"},
    "n_per_group": {"type": "integer", "min": 5, "max": 60, "label": "三层模型每组样本量"},
    "as_of": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "label": "三态评估基准日"},
}


def normalize_params(p: dict) -> dict:
    """参数校验与归一：非法值退回默认，保证策略调整不会把系统带崩。"""
    out = dict(DEFAULT_PARAMS)
    if not isinstance(p, dict):
        return out
    if p.get("industry") in PARAM_SCHEMA["industry"]["enum"]:
        out["industry"] = p["industry"]
    if p.get("scenario") in PARAM_SCHEMA["scenario"]["enum"]:
        out["scenario"] = p["scenario"]
    try:
        amt = float(p.get("amount"))
        if amt >= 0:
            out["amount"] = amt
    except Exception:
        pass
    try:
        n = int(p.get("n_per_group"))
        if 5 <= n <= 60:
            out["n_per_group"] = n
    except Exception:
        pass
    if p.get("as_of"):
        out["as_of"] = str(p["as_of"])[:10]
    return out


def sse_frame(data: dict) -> bytes:
    """把事件编码为一帧 SSE（UTF-8）。"""
    import json as _json
    return ("data: %s\n\n"
            % _json.dumps(data, ensure_ascii=False, default=str)).encode("utf-8")
