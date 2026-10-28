# -*- coding: utf-8 -*-
"""能力 5：决策卡片 —— 面向企业工作者，输出「背景 / 结论 / 建议动作 / 风险提示」。

只做结构化呈现，不替人做决定；每条结论都必须能追溯到指标或巡检项。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List

LEVEL_CN = {"high": "高", "medium": "中", "low": "低"}

# 结论 → 建议动作 / 风险提示 的规则表（可配置化后移到 config）
PLAYBOOK = {
    "source_missing": {
        "action": "核对数据源配置与表结构，补齐缺失来源后重新巡检；期间不要依据不完整数据下结论。",
        "risk": "缺失来源上的指标被排除在判定之外，结论覆盖率不完整，存在漏判风险。",
    },
    "net_flow_negative": {
        "action": "立即拉出期间大额流出明细，区分刚性支出与可延后支出，安排未来两周资金排程。",
        "risk": "若净流出持续，账面现金将逐期下降，需同步评估授信额度是否充足。",
    },
    "buffer_ratio_high": {
        "action": "压缩非刚性付款、加快应收账款催收，必要时启动备用授信。",
        "risk": "现金缓冲触及高危线，任何一笔大额意外支出都可能造成支付违约。",
    },
    "buffer_ratio_warn": {
        "action": "列入本周关注清单，复核未来 30 天付款计划与回款计划。",
        "risk": "缓冲比已进入预警区间，若回款延期将快速滑向高危。",
    },
    "overdue_ratio_high": {
        "action": "对逾期客户分级：暂停新增授信、启动催收与法务预案，评估坏账计提。",
        "risk": "逾期占比过高会同时冲击现金流与信用评级，且存在连锁违约可能。",
    },
    "overdue_ratio_warn": {
        "action": "对逾期账户逐户落实责任人与回款时间点，纳入下周期复查。",
        "risk": "逾期率处于上行通道，若不干预大概率进入高危区间。",
    },
    "all_clear": {
        "action": "维持常规巡检节奏，无需额外动作。",
        "risk": "结论基于当前阈值与现有数据源；阈值或数据口径变化后需重新评估。",
    },
}


class DecisionCardBuilder:
    def __init__(self, db, cfg, datasource, logger):
        self.db = db
        self.cfg = cfg
        self.ds = datasource
        self.log = logger

    def build(self, trigger: str = "manual") -> Dict[str, Any]:
        from .risk import evaluate  # 局部导入避免循环

        try:
            metrics = self.ds.metrics()
        except Exception as exc:  # noqa: BLE001
            metrics = {"missing_sources": [{"source": "unknown", "reason": str(exc)}]}
        verdict = evaluate(metrics, self.cfg)
        reasons: List[Dict[str, Any]] = verdict.get("reasons", [])

        background = self._background(metrics)
        conclusion = self._conclusion(verdict)
        actions = self._actions(reasons)
        risks = self._risks(reasons)

        card = {
            "card_id": "CARD-" + uuid.uuid4().hex[:16],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "level": verdict["level"],
            "level_cn": LEVEL_CN.get(verdict["level"], verdict["level"]),
            "background": background,
            "conclusion": conclusion,
            "suggested_actions": actions,
            "risk_notes": risks,
            "evidence": reasons,
            "metrics": metrics,
        }
        return card

    def _background(self, metrics: Dict[str, Any]) -> str:
        parts = []
        if metrics.get("net_flow") is not None:
            parts.append(f"期间净现金流 {metrics['net_flow']}")
        if metrics.get("closing_balance") is not None:
            parts.append(f"期末余额 {metrics['closing_balance']}")
        if metrics.get("buffer_ratio") is not None:
            parts.append(f"现金缓冲比 {metrics['buffer_ratio']}")
        if metrics.get("overdue_ratio") is not None:
            parts.append(f"逾期应收占比 {metrics['overdue_ratio']}")
        if metrics.get("receivable_count") is not None:
            parts.append(f"在管应收 {metrics['receivable_count']} 笔")
        if metrics.get("missing_sources"):
            parts.append("数据源缺失：" + "、".join(
                f"{m.get('source')}" for m in metrics["missing_sources"]))
        return "；".join(parts) if parts else "当前无可用的量化背景数据"

    def _conclusion(self, verdict: Dict[str, Any]) -> str:
        lv = LEVEL_CN.get(verdict["level"], verdict["level"])
        top = [r for r in verdict.get("reasons", []) if r.get("level") == verdict["level"]]
        if top:
            return f"风险等级 {lv}。" + "；".join(r["text"] for r in top)
        return f"风险等级 {lv}。各项指标均在阈值内。"

    def _actions(self, reasons: List[Dict[str, Any]]) -> List[str]:
        out = []
        for r in reasons:
            item = PLAYBOOK.get(r.get("code"))
            if item and item["action"] not in out:
                out.append(item["action"])
        return out or ["暂无可自动给出的动作建议，请人工复核指标口径。"]

    def _risks(self, reasons: List[Dict[str, Any]]) -> List[str]:
        out = []
        for r in reasons:
            item = PLAYBOOK.get(r.get("code"))
            if item and item["risk"] not in out:
                out.append(item["risk"])
        return out or ["结论依赖当前配置阈值；阈值调整后需重新生成卡片。"]

    def run(self, trigger: str = "manual", persist: bool = True) -> Dict[str, Any]:
        card = self.build(trigger=trigger)
        if persist:
            t = self.cfg.table
            try:
                self.db.insert(
                    f"INSERT INTO `{t['card']}` "
                    "(card_id, level, trigger_type, content_json, created_at) "
                    "VALUES (%s,%s,%s,%s,NOW())",
                    (card["card_id"], card["level"], trigger,
                     json.dumps(card, ensure_ascii=False, default=str)[:60000]),
                )
            except Exception as exc:  # noqa: BLE001
                # 留痕失败不应让卡片无法返回，但必须记录
                self.log.error("决策卡片留痕失败：%s: %s", type(exc).__name__, exc)
                card["persist_error"] = f"{type(exc).__name__}: {exc}"
        return card

    def render_text(self, card: Dict[str, Any]) -> str:
        lines = [
            f"【决策卡片 · 风险等级 {card['level_cn']}】 {card['card_id']}",
            "",
            f"背景：{card['background']}",
            "",
            f"结论：{card['conclusion']}",
            "",
            "建议动作：",
        ]
        lines += [f"  {i}. {a}" for i, a in enumerate(card["suggested_actions"], 1)]
        lines.append("")
        lines.append("风险提示：")
        lines += [f"  - {r}" for r in card["risk_notes"]]
        return "\n".join(lines)
