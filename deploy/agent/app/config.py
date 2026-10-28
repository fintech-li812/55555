# -*- coding: utf-8 -*-
"""配置加载：非敏感配置放 config.yaml/json，敏感项只从环境变量（EnvironmentFile）读。

原则：
  1. 配置文件里绝不出现口令、令牌、Webhook；这些只能来自 /etc/cashflow-agent/agent.env。
  2. 关键项缺失必须显式报错，不允许用"看起来能用"的默认值蒙混。
  3. 支持 .yaml 与 .json 两种格式（PyYAML 缺失时自动降级 JSON）。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from .errors import AppError, ERR_CONFIG, ERR_CONFIG_SECRET

DEFAULT_CONFIG_PATH = "/etc/cashflow-agent/config.yaml"


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_file(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise AppError(ERR_CONFIG, "配置文件不存在", {"path": path})
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            # 降级：yaml 不可用时按 JSON 解析，配置文件可改用 .json
            return json.loads(text)
        return yaml.safe_load(text) or {}
    return json.loads(text or "{}")


class Config:
    """已校验的运行时配置。构造后只读。"""

    def __init__(self, raw: Dict[str, Any], env: Dict[str, str] | None = None):
        self.raw = raw
        self.env = env if env is not None else dict(os.environ)

        srv = raw.get("server") or {}
        self.host = srv.get("host", "127.0.0.1")
        self.port = _as_int(srv.get("port"), 8090)
        self.api_token = self.env.get("CF_AGENT_API_TOKEN", "").strip()

        db = raw.get("database") or {}
        self.db_host = db.get("host", "127.0.0.1")
        self.db_port = _as_int(db.get("port"), 3306)
        self.db_name = db.get("name", "cf_workbench")
        self.db_user = self.env.get("CF_DB_USER", db.get("user", "cf_agent")).strip()
        self.db_password = self.env.get("CF_DB_PASSWORD", "")
        self.db_connect_timeout = _as_int(db.get("connect_timeout"), 5)
        self.db_retry_times = _as_int(db.get("retry_times"), 3)
        self.db_retry_backoff = _as_float(db.get("retry_backoff"), 1.5)
        self.table_prefix = db.get("table_prefix", "agent_")

        sch = raw.get("schedule") or {}
        self.inspect_interval_minutes = _as_int(sch.get("inspect_interval_minutes"), 60)
        self.weekly_day = _as_int(sch.get("weekly_day"), 1)      # 1=周一
        self.weekly_hour = _as_int(sch.get("weekly_hour"), 9)
        self.weekly_minute = _as_int(sch.get("weekly_minute"), 0)

        th = raw.get("thresholds") or {}
        self.buffer_ratio_warn = _as_float(th.get("buffer_ratio_warn"), 0.15)
        self.buffer_ratio_high = _as_float(th.get("buffer_ratio_high"), 0.08)
        self.overdue_ratio_warn = _as_float(th.get("overdue_ratio_warn"), 0.20)
        self.overdue_ratio_high = _as_float(th.get("overdue_ratio_high"), 0.35)
        self.net_flow_floor = _as_float(th.get("net_flow_floor"), 0.0)

        ch = raw.get("channels") or {}
        self.channel_type = ch.get("type", "none")   # none / dingtalk / webhook
        self.dingtalk_webhook = self.env.get("DINGTALK_WEBHOOK", "").strip()
        self.dingtalk_secret = self.env.get("DINGTALK_SECRET", "").strip()
        self.webhook_url = self.env.get("AGENT_WEBHOOK_URL", "").strip()
        self.webhook_token = self.env.get("AGENT_WEBHOOK_TOKEN", "").strip()

        src = raw.get("sources") or {}
        self.source_tables = {
            "cashflow": src.get("cashflow_table", "mfg_cashflow_runs_v1"),
            "receivable": src.get("receivable_table", "mfg_v8_tracking_v1"),
            "risk": src.get("risk_table", "mfg_v8_risk_log_v1"),
        }

        paths = raw.get("paths") or {}
        self.log_dir = paths.get("log_dir", "/var/log/cashflow-agent")
        self.state_dir = paths.get("state_dir", "/var/lib/cashflow-agent")

        log = raw.get("logging") or {}
        self.log_level = log.get("level", "INFO")
        self.log_max_bytes = _as_int(log.get("max_bytes"), 20 * 1024 * 1024)
        self.log_backup_count = _as_int(log.get("backup_count"), 5)

        self._validate()

    def _validate(self):
        if not (1 <= self.port <= 65535):
            raise AppError(ERR_CONFIG, "server.port 非法", {"port": self.port})
        if not self.db_user:
            raise AppError(ERR_CONFIG_SECRET, "缺少数据库用户（CF_DB_USER 未设置）")
        # 口令允许为空仅当数据库使用 socket 免密；这里显式提示，不静默
        if not self.db_password:
            raise AppError(
                ERR_CONFIG_SECRET,
                "缺少数据库口令（CF_DB_PASSWORD 未设置）；如确需免密，请在配置中显式声明",
            )
        if not self.api_token:
            raise AppError(ERR_CONFIG_SECRET, "缺少接口令牌（CF_AGENT_API_TOKEN 未设置）")
        if self.inspect_interval_minutes < 1:
            raise AppError(ERR_CONFIG, "schedule.inspect_interval_minutes 必须 >= 1")

    @property
    def table(self) -> Dict[str, str]:
        """带前缀的业务表名。"""
        p = self.table_prefix
        return {
            "inspection": p + "inspection",
            "inspection_item": p + "inspection_item",
            "report": p + "report",
            "alert": p + "alert",
            "chat_log": p + "chat_log",
            "card": p + "card",
        }

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        p = path or os.environ.get("CF_AGENT_CONFIG", DEFAULT_CONFIG_PATH)
        return cls(_load_file(p))

    def public_view(self) -> Dict[str, Any]:
        """可安全打印的配置快照（永不含口令/令牌）。"""
        return {
            "server": {"host": self.host, "port": self.port},
            "database": {
                "host": self.db_host, "port": self.db_port,
                "name": self.db_name, "user": self.db_user,
            },
            "schedule": {
                "inspect_interval_minutes": self.inspect_interval_minutes,
                "weekly": f"day={self.weekly_day} {self.weekly_hour:02d}:{self.weekly_minute:02d}",
            },
            "thresholds": {
                "buffer_ratio_warn": self.buffer_ratio_warn,
                "buffer_ratio_high": self.buffer_ratio_high,
                "overdue_ratio_warn": self.overdue_ratio_warn,
                "overdue_ratio_high": self.overdue_ratio_high,
            },
            "channels": {
                "type": self.channel_type,
                "dingtalk_configured": bool(self.dingtalk_webhook),
                "webhook_configured": bool(self.webhook_url),
            },
            "sources": self.source_tables,
            "paths": {"log_dir": self.log_dir, "state_dir": self.state_dir},
        }
