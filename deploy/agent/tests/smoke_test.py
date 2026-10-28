#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线冒烟测试：不连数据库、不起端口，验证核心逻辑。

用法： python3 tests/smoke_test.py
依赖： 仅需标准库（不 import pymysql / yaml）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app.config import Config          # noqa: E402
from app import risk, scheduler        # noqa: E402
from app.decision_card import DecisionCardBuilder  # noqa: E402
from app.chat import ChatEngine        # noqa: E402
from app.inspector import Inspector    # noqa: E402
from app.reporter import Reporter      # noqa: E402
from app.channels import build as build_channel  # noqa: E402
from app.errors import AppError        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))


class FakeDB:
    """最小替身：只记录写入，不触碰真实数据库。"""

    def __init__(self, tables=("mfg_cashflow_runs_v1", "mfg_v8_tracking_v1")):
        self.tables = set(tables)
        self.writes = []

    def table_exists(self, name):
        return name in self.tables

    def query(self, sql, args=None):
        return [{"level": "high", "c": 2}, {"level": "low", "c": 5}]

    def insert(self, sql, args=None):
        if not sql.lstrip().upper().startswith(("INSERT", "REPLACE")):
            raise AssertionError("只允许 INSERT")
        self.writes.append((sql, args))
        return len(self.writes)


class FakeDS:
    def __init__(self, metrics):
        self.metrics_ = metrics

    def metrics(self):
        return self.metrics_

    def overview(self, limit=20):
        return {"available": True, "table": "t", "latest": {"net_flow": -1000}, "recent": []}


def make_cfg(tmp):
    raw = {
        "server": {"host": "127.0.0.1", "port": 8090},
        "database": {"host": "127.0.0.1", "port": 3306, "name": "cf_workbench"},
        "schedule": {"inspect_interval_minutes": 60, "weekly_day": 1,
                     "weekly_hour": 9, "weekly_minute": 0},
        "thresholds": {"net_flow_floor": 0.0, "buffer_ratio_warn": 0.15,
                       "buffer_ratio_high": 0.08, "overdue_ratio_warn": 0.20,
                       "overdue_ratio_high": 0.35},
        "channels": {"type": "none"},
        "sources": {"cashflow_table": "mfg_cashflow_runs_v1",
                    "receivable_table": "mfg_v8_tracking_v1",
                    "risk_table": "mfg_v8_risk_log_v1"},
        "paths": {"log_dir": os.path.join(tmp, "log"), "state_dir": os.path.join(tmp, "state")},
        "logging": {"level": "INFO"},
    }
    env = {"CF_DB_USER": "cf_agent", "CF_DB_PASSWORD": "x", "CF_AGENT_API_TOKEN": "t"}
    return Config(raw, env)


def main():
    tmp = tempfile.mkdtemp(prefix="cfagent-smoke-")
    cfg = make_cfg(tmp)
    print("\n== 1. 配置与脱敏 ==")
    check("配置加载成功", cfg.db_user == "cf_agent" and cfg.port == 8090)
    pub = cfg.public_view()
    check("配置快照不含口令/令牌",
          "password" not in str(pub).lower() and "token" not in str(pub).lower())
    try:
        Config({"server": {"port": 1}, "paths": cfg.public_view()["paths"]}, {})
        check("缺少令牌时应报错", False)
    except AppError as e:
        check("缺少令牌时应报错", e.code == 1002, f"code={e.code}")

    print("\n== 2. 风险判定 ==")
    m_bad = {"net_flow": -1000, "buffer_ratio": 0.05, "overdue_ratio": 0.40}
    check("高危场景判为 high", risk.evaluate(m_bad, cfg)["level"] == "high")
    m_mid = {"net_flow": 100, "buffer_ratio": 0.12, "overdue_ratio": 0.25}
    check("预警场景判为 medium", risk.evaluate(m_mid, cfg)["level"] == "medium")
    m_ok = {"net_flow": 1000, "buffer_ratio": 0.5, "overdue_ratio": 0.05}
    check("正常场景判为 low", risk.evaluate(m_ok, cfg)["level"] == "low")
    m_miss = {"missing_sources": [{"source": "cashflow", "reason": "源表不存在"}]}
    check("数据缺失不得判为 low", risk.evaluate(m_miss, cfg)["level"] == "medium")

    print("\n== 3. 通道 dry-run ==")
    ch = build_channel(cfg, __import__("logging").getLogger("smoke"))
    res = ch.send("t", "b")
    check("未配置通道时 status=not_configured", res["status"] == "not_configured")
    check("未配置通道时不得声称已发送", res["sent"] is False and res["message_id"] is None)

    print("\n== 4. 巡检 ==")
    db = FakeDB()
    insp = Inspector(db, cfg, FakeDS(m_bad), ch, __import__("logging").getLogger("smoke"))
    r = insp.run(trigger="test")
    check("巡检返回 high", r["level"] == "high")
    check("巡检写入主记录+明细", len(db.writes) >= 5, f"writes={len(db.writes)}")
    check("巡检全部为 INSERT", all(w[0].lstrip().upper().startswith("INSERT") for w in db.writes))
    check("告警留痕为 not_configured", r["alert"]["status"] == "not_configured")

    print("\n== 5. 决策卡片 ==")
    cb = DecisionCardBuilder(db, cfg, FakeDS(m_bad), __import__("logging").getLogger("smoke"))
    card = cb.run(trigger="test", persist=True)
    for k in ("background", "conclusion", "suggested_actions", "risk_notes"):
        check(f"卡片含字段 {k}", bool(card.get(k)))
    check("卡片留痕已写入", any("agent_card" in w[0] for w in db.writes))
    txt = cb.render_text(card)
    check("卡片可渲染为文本", "建议动作" in txt and "风险提示" in txt)

    print("\n== 6. 对话意图 ==")
    engine = ChatEngine(db, cfg, FakeDS(m_ok), insp,
                        Reporter(db, cfg, FakeDS(m_ok), insp, ch,
                                 __import__("logging").getLogger("smoke")),
                        cb, __import__("logging").getLogger("smoke"))
    cases = {"现在巡检一次": "inspect", "给我本周周报": "weekly",
             "给我决策建议": "card", "逾期多少": "receivable",
             "现金流什么情况": "overview", "帮助": "help"}
    for q, want in cases.items():
        check(f"意图识别「{q}」→ {want}", engine.detect(q) == want)
    txt, meta = engine.answer("现在巡检一次")
    check("对话返回真实巡检结果", "巡检完成" in txt, meta["intent"])

    print("\n== 7. 调度器 ==")
    sch = scheduler.Scheduler(cfg, __import__("logging").getLogger("smoke"),
                              on_inspect=lambda: None, on_weekly=lambda: None,
                              tick_seconds=5)
    check("首次巡检应触发", sch._due_inspect(__import__("datetime").datetime.now()) is True)
    sch.state["last_inspect_ts"] = time.time()
    check("刚跑过则不重复触发", sch._due_inspect(__import__("datetime").datetime.now()) is False)

    print("\n" + "=" * 60)
    print(f"结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
    print("全部通过（离线，未连接任何数据库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
