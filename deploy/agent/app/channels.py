# -*- coding: utf-8 -*-
"""推送通道适配器（告警 / 周报）。

铁律：未配置凭据时一律 dry-run，status 写 "not_configured"，message_id 为 None。
绝不伪造"已送达"，绝不把 dry-run 记成 success。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict


class BaseChannel:
    name = "none"

    def send(self, title: str, body: str, meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
        raise NotImplementedError


class DryRunChannel(BaseChannel):
    """未配置任何通道：只落库 + 写日志，不对外发送。"""

    name = "dry-run"

    def __init__(self, logger):
        self.log = logger

    def send(self, title, body, meta=None) -> Dict[str, Any]:
        self.log.info("[dry-run] 未配置推送通道，内容仅留痕。title=%s", title)
        return {
            "ok": True, "sent": False, "channel": self.name,
            "status": "not_configured", "message_id": None,
            "detail": "未配置 DINGTALK_WEBHOOK / AGENT_WEBHOOK_URL，未实际发送",
        }


class DingTalkChannel(BaseChannel):
    """钉钉群机器人（支持加签）。"""

    name = "dingtalk"

    def __init__(self, webhook: str, secret: str, logger, timeout: int = 8):
        self.webhook = webhook
        self.secret = secret or ""
        self.log = logger
        self.timeout = timeout

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook}&timestamp={ts}&sign={sign}"

    def send(self, title, body, meta=None) -> Dict[str, Any]:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": body},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._signed_url(), data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            parsed = json.loads(raw)
            if parsed.get("errcode") == 0:
                return {"ok": True, "sent": True, "channel": self.name,
                        "status": "sent", "message_id": None, "detail": parsed}
            return {"ok": False, "sent": False, "channel": self.name,
                    "status": "failed", "message_id": None,
                    "detail": f"errcode={parsed.get('errcode')} errmsg={parsed.get('errmsg')}"}
        except Exception as exc:  # noqa: BLE001
            self.log.error("钉钉推送失败：%s: %s", type(exc).__name__, exc)
            return {"ok": False, "sent": False, "channel": self.name,
                    "status": "failed", "message_id": None,
                    "detail": f"{type(exc).__name__}: {exc}"}


class WebhookChannel(BaseChannel):
    """通用 Webhook（企业自建网关 / 内部系统）。"""

    name = "webhook"

    def __init__(self, url: str, token: str, logger, timeout: int = 8):
        self.url = url
        self.token = token or ""
        self.log = logger
        self.timeout = timeout

    def send(self, title, body, meta=None) -> Dict[str, Any]:
        payload = {"title": title, "body": body, "meta": meta or {}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return {"ok": True, "sent": True, "channel": self.name,
                    "status": "sent", "message_id": None, "detail": raw[:500]}
        except Exception as exc:  # noqa: BLE001
            self.log.error("Webhook 推送失败：%s: %s", type(exc).__name__, exc)
            return {"ok": False, "sent": False, "channel": self.name,
                    "status": "failed", "message_id": None,
                    "detail": f"{type(exc).__name__}: {exc}"}


def build(cfg, logger) -> BaseChannel:
    t = (cfg.channel_type or "none").lower()
    if t == "dingtalk" and cfg.dingtalk_webhook:
        return DingTalkChannel(cfg.dingtalk_webhook, cfg.dingtalk_secret, logger)
    if t in ("webhook", "generic") and cfg.webhook_url:
        return WebhookChannel(cfg.webhook_url, cfg.webhook_token, logger)
    if t not in ("none", "dingtalk", "webhook", "generic"):
        logger.warning("未知通道类型 %s，回退为 dry-run", t)
    return DryRunChannel(logger)
