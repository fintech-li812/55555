"""Outbound notification only. Never accepts inbound control, never rewrites amounts.

Rules that must not be broken:
- Text is built exclusively from fields present in the engine result. A missing field
  renders as "未返回"; it is never filled in, estimated, or carried over from a previous run.
- SYNTHETIC marking is mandatory whenever the source is synthetic.
- A notification failure never fails the calculation. The caller decides what to do.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SEC = 5

GROUP_NAMES = {"operating": "经营活动", "investing": "投资活动", "financing": "筹资活动"}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def channels() -> dict:
    """Report which channels are configured. Never returns secrets."""
    return {
        "dingtalk": bool(_env("DINGTALK_WEBHOOK")),
        "dingtalk_signed": bool(_env("DINGTALK_SECRET")),
        "wecom": bool(_env("WECOM_WEBHOOK")),
    }


def _amount(result: dict, key: str) -> str:
    amounts = result.get("amounts") or {}
    value = amounts.get(key)
    if value is None:
        return "未返回"
    try:
        return "{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "未返回"


def format_calculation(payload: dict) -> str:
    """Build the push text from one calculate() response. Engine output only."""
    result = payload.get("result") or {}
    status = payload.get("status") or "未返回"
    source = result.get("source") or {}
    source_id = source.get("source_id") or payload.get("source_id") or "未返回"
    groups = result.get("cashflow_groups") or {}

    lines = [
        "### 制造业现金流工作台（程序计算）",
        "",
        "- 来源：`{}`".format(source_id),
        "- 期间：{}".format(result.get("period") or "未返回"),
        "- 状态：{}".format(status),
        "- 运行编号：`{}`".format(result.get("run_id") or "未返回"),
    ]
    if groups:
        for key, name in GROUP_NAMES.items():
            values = groups.get(key)
            if not values:
                continue
            lines.append(
                "- {}：流入 {} / 流出 {} / 净额 {}".format(
                    name,
                    _amount_like(values.get("inflow")),
                    _amount_like(values.get("outflow")),
                    _amount_like(values.get("net")),
                )
            )
    lines += [
        "",
        "| 项目 | 金额（元） |",
        "| --- | --- |",
        "| 期初余额 | {} |".format(_amount(result, "opening")),
        "| 流入合计 | {} |".format(_amount(result, "inflow")),
        "| 流出合计 | {} |".format(_amount(result, "outflow")),
        "| 计算期末 | {} |".format(_amount(result, "closing")),
        "| 差异 | {} |".format(_amount(result, "difference")),
        "",
        "- 余额核对：{}".format(_reconciliation_text(result.get("reconciliation_state"))),
        "- 可保存：{}".format("是" if payload.get("can_save_source_result") else "否"),
    ]
    execution = result.get("execution") or {}
    if execution.get("algorithm_sha256"):
        lines.append("- 算法哈希：`{}`".format(str(execution["algorithm_sha256"])[:16]))
    lines += [
        "",
        "> 本条由确定性程序生成。全部数值为合成演示数据（SYNTHETIC），"
        "不代表任何真实企业口径，未通过真实企业验证。",
    ]
    return "\n".join(lines)


def _amount_like(value) -> str:
    if value is None:
        return "未返回"
    try:
        return "{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "未返回"


def _reconciliation_text(state) -> str:
    return {
        "matched": "一致",
        "difference_pending_review": "存在差异，待核实，已阻止保存",
    }.get(state, str(state or "未返回"))


def _post(url: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return {"ok": True, "status": response.status, "body": response.read().decode("utf-8", "replace")[:500]}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "body": exc.read().decode("utf-8", "replace")[:500]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "body": "{}: {}".format(type(exc).__name__, exc)}


def push_dingtalk(text: str) -> dict:
    """DingTalk custom robot. Signing is applied when DINGTALK_SECRET is present."""
    webhook = _env("DINGTALK_WEBHOOK")
    if not webhook:
        return {"channel": "dingtalk", "sent": False, "reason": "not_configured"}
    url = webhook
    secret = _env("DINGTALK_SECRET")
    if secret:
        stamp = str(int(_now() * 1000))
        sign = hmac.new(secret.encode("utf-8"), "{}\n{}".format(stamp, secret).encode("utf-8"), hashlib.sha256).digest()
        url = "{}&timestamp={}&sign={}".format(
            webhook, stamp, urllib.parse.quote(base64.b64encode(sign).decode("utf-8"))
        )
    outcome = _post(url, {"msgtype": "markdown", "markdown": {"title": "现金流计算结果", "text": text}})
    return {"channel": "dingtalk", "sent": outcome["ok"], "status": outcome["status"], "body": outcome["body"]}


def push_wecom(text: str) -> dict:
    """WeCom group robot."""
    webhook = _env("WECOM_WEBHOOK")
    if not webhook:
        return {"channel": "wecom", "sent": False, "reason": "not_configured"}
    outcome = _post(webhook, {"msgtype": "markdown", "markdown": {"content": text}})
    return {"channel": "wecom", "sent": outcome["ok"], "status": outcome["status"], "body": outcome["body"]}


def push(payload: dict) -> dict:
    """Push one calculate() response to every configured channel."""
    if payload.get("status") == "blocked":
        text = "\n".join(
            [
                "### 制造业现金流工作台（程序计算）",
                "",
                "- 状态：blocked（本次未产生完整结果，也没有写库）",
                "- 来源：`{}`".format(payload.get("source_id") or "未返回"),
                "> 合成演示数据（SYNTHETIC），不代表任何真实企业口径。",
            ]
        )
    else:
        text = format_calculation(payload)
    return {"text": text, "results": [push_dingtalk(text), push_wecom(text)]}


def _now() -> float:
    import time

    return time.time()
