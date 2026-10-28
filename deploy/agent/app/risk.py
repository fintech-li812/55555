# -*- coding: utf-8 -*-
"""风险判定（精简确定性规则，阈值全部来自配置）。

刻意不使用机器学习模型：2G 内存机器上常驻 sklearn 不划算，
且本层要的是"可解释、可复核"的分级，而不是概率。
"""
from __future__ import annotations

from typing import Any, Dict, List

LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}


def _max(a: str, b: str) -> str:
    return a if LEVEL_ORDER.get(a, 0) >= LEVEL_ORDER.get(b, 0) else b


def evaluate(metrics: Dict[str, Any], cfg) -> Dict[str, Any]:
    """输入指标，输出 {level, reasons[]}。缺指标 → 记为 unknown 而非假定安全。"""
    reasons: List[Dict[str, Any]] = []
    level = "low"

    missing = metrics.get("missing_sources") or []
    if missing:
        level = _max(level, "medium")
        reasons.append({
            "code": "source_missing",
            "level": "medium",
            "text": "部分数据源不可用：" + "、".join(
                f"{m.get('source')}({m.get('reason')})" for m in missing),
        })

    net = metrics.get("net_flow")
    if net is not None:
        if net < cfg.net_flow_floor:
            level = _max(level, "high")
            reasons.append({
                "code": "net_flow_negative",
                "level": "high",
                "text": f"期间净现金流为 {net}，低于阈值 {cfg.net_flow_floor}",
            })

    br = metrics.get("buffer_ratio")
    if br is not None:
        if br <= cfg.buffer_ratio_high:
            level = _max(level, "high")
            reasons.append({
                "code": "buffer_ratio_high",
                "level": "high",
                "text": f"现金缓冲比 {br} ≤ 高危阈值 {cfg.buffer_ratio_high}",
            })
        elif br <= cfg.buffer_ratio_warn:
            level = _max(level, "medium")
            reasons.append({
                "code": "buffer_ratio_warn",
                "level": "medium",
                "text": f"现金缓冲比 {br} ≤ 预警阈值 {cfg.buffer_ratio_warn}",
            })

    orr = metrics.get("overdue_ratio")
    if orr is not None:
        if orr >= cfg.overdue_ratio_high:
            level = _max(level, "high")
            reasons.append({
                "code": "overdue_ratio_high",
                "level": "high",
                "text": f"逾期应收占比 {orr} ≥ 高危阈值 {cfg.overdue_ratio_high}",
            })
        elif orr >= cfg.overdue_ratio_warn:
            level = _max(level, "medium")
            reasons.append({
                "code": "overdue_ratio_warn",
                "level": "medium",
                "text": f"逾期应收占比 {orr} ≥ 预警阈值 {cfg.overdue_ratio_warn}",
            })

    if not reasons:
        reasons.append({"code": "all_clear", "level": "low", "text": "各项指标均在阈值内"})

    return {"level": level, "reasons": reasons}
