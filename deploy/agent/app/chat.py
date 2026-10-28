# -*- coding: utf-8 -*-
"""能力 4：前端对话 —— 把自然语言请求路由到确定性能力。

设计边界：
  - 本 agent 不做开放式闲聊，也不调用外部大模型；
  - 命中意图 → 调用对应确定性能力（巡检/周报/卡片/汇总），返回真实数据；
  - 未命中 → 返回可用能力清单，绝不编造答案。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .decision_card import DecisionCardBuilder


class ChatEngine:
    def __init__(self, db, cfg, datasource, inspector, reporter, cards: DecisionCardBuilder, logger):
        self.db = db
        self.cfg = cfg
        self.ds = datasource
        self.inspector = inspector
        self.reporter = reporter
        self.cards = cards
        self.log = logger

    # ---------- 意图识别 ----------
    @staticmethod
    def detect(text: str) -> str:
        t = (text or "").strip().lower()
        if any(k in t for k in ("巡检", "检查", "inspect", "巡查")):
            return "inspect"
        if any(k in t for k in ("周报", "weekly", "本周汇总", "报告")):
            return "weekly"
        if any(k in t for k in ("决策", "建议", "卡片", "怎么办", "card", "动作")):
            return "card"
        if any(k in t for k in ("应收", "逾期", "receivable", "回款")):
            return "receivable"
        if any(k in t for k in ("概览", "汇总", "现金流", "overview", "情况", "怎么样")):
            return "overview"
        if any(k in t for k in ("帮助", "help", "能做什么", "怎么用")):
            return "help"
        return "unknown"

    # ---------- 应答 ----------
    def answer(self, message: str, session_id: str = "") -> Tuple[str, Dict[str, Any]]:
        sid = session_id or ("S-" + uuid.uuid4().hex[:8])
        intent = self.detect(message)
        started = datetime.now()

        try:
            if intent == "inspect":
                res = self.inspector.run(trigger="chat")
                text = self._fmt_inspect(res)
            elif intent == "weekly":
                res = self.reporter.run(days=7, push=False)
                text = res.get("markdown") or "周报生成失败"
            elif intent == "card":
                card = self.cards.run(trigger="chat")
                text = self.cards.render_text(card)
            elif intent == "receivable":
                res = self.ds.receivables()
                text = self._fmt_receivable(res)
            elif intent == "overview":
                res = self.ds.overview()
                text = self._fmt_overview(res)
            elif intent == "help":
                text = self._help()
                res = {}
            else:
                res = {}
                text = ("未识别出明确意图。我可以做这些：\n" + self._help())
        except Exception as exc:  # noqa: BLE001
            self.log.error("对话处理失败：%s: %s", type(exc).__name__, exc)
            text = f"处理失败：{type(exc).__name__}: {exc}（已记录日志，未编造结果）"
            res = {"error": str(exc)}
            intent = "error"

        duration = int((datetime.now() - started).total_seconds() * 1000)
        self._log(sid, intent, message, text, duration)
        return text, {"session_id": sid, "intent": intent, "duration_ms": duration}

    # ---------- 渲染 ----------
    @staticmethod
    def _fmt_overview(r: Dict[str, Any]) -> str:
        latest = r.get("latest") or {}
        items = [f"{k}={v}" for k, v in list(latest.items())[:12]]
        return (f"现金流概览（源表 {r.get('table')}，共 {r.get('row_count')} 条记录）\n"
                "最新一条：\n  " + "\n  ".join(items))

    @staticmethod
    def _fmt_receivable(r: Dict[str, Any]) -> str:
        by_state = r.get("by_state") or {}
        state_txt = "、".join(f"{k} {v} 笔" for k, v in by_state.items()) or "无"
        return (f"应收概况（共 {r.get('count')} 笔）\n"
                f"  状态分布：{state_txt}\n"
                f"  应收总额：{r.get('amount_total')}\n"
                f"  逾期金额：{r.get('overdue_amount')}（占比 {r.get('overdue_ratio')}）")

    @staticmethod
    def _fmt_inspect(r: Dict[str, Any]) -> str:
        lines = [f"巡检完成：{r.get('inspection_id')}（等级 {r.get('level')}，"
                 f"{r.get('item_count')} 项，未通过 {r.get('failed_count')} 项，"
                 f"耗时 {r.get('duration_ms')} ms）"]
        for it in r.get("items", []):
            flag = "通过" if it["passed"] else "未通过"
            lines.append(f"  [{flag}] {it['name']}：{it['detail']}")
        alert = r.get("alert")
        if alert:
            lines.append(f"告警：{alert.get('status')}（通道 {alert.get('channel')}）")
        return "\n".join(lines)

    @staticmethod
    def _help() -> str:
        return ("  1. 触发一次巡检 —— 例：「现在巡检一次」\n"
                "  2. 生成本周周报 —— 例：「给我本周周报」\n"
                "  3. 生成决策卡片 —— 例：「给我决策建议」\n"
                "  4. 查看现金流概览 —— 例：「现在现金流什么情况」\n"
                "  5. 查看应收与逾期 —— 例：「逾期多少」")

    # ---------- 留痕 ----------
    def _log(self, sid: str, intent: str, question: str, answer: str, duration: int):
        try:
            t = self.cfg.table
            self.db.insert(
                f"INSERT INTO `{t['chat_log']}` "
                "(session_id, intent, question, answer, duration_ms, created_at) "
                "VALUES (%s,%s,%s,%s,%s,NOW())",
                (sid, intent, question[:2000], answer[:8000], duration),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error("对话留痕失败：%s: %s", type(exc).__name__, exc)
