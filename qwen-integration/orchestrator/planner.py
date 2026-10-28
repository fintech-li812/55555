# -*- coding: utf-8 -*-
"""本地编排器：意图解析 → 执行计划（步骤 DAG）。

定位：
1. 千问办公可用时，本模块只做**计划校验器**（校验千问返回的 plan 是否合法、工具是否存在）；
2. 千问未配置或调用失败时，本模块作为**兜底编排**，保证链路不断（前端仍可完整跑通）；
3. 任何计划中的工具必须存在于 MCP tools/list，否则拒绝执行（不凭空造工具）。
"""
from __future__ import annotations

from . import protocol as P


# 意图 → 关键词 → 步骤序列
INTENTS = [
    {
        "intent": "cashflow.calc", "name": "现金流计算与核对",
        "keywords": ("现金流", "计算", "核对", "对账", "三类", "余额"),
        "steps": [{"tool": "cashflow.calculate", "desc": "计算三类现金流与独立余额核对",
                   "args": lambda p: {"source_id": "synthetic_eight_sheet_v1"}}],
    },
    {
        "intent": "risk.assess", "name": "三层风控评估",
        "keywords": ("风险", "评估", "评级", "三层", "benford", "dea", "判别", "舞弊", "预警"),
        "steps": [{"tool": "risk.assess_v8", "desc": "三层模型评估 + 缓冲取严 + 压力测试 + 审批",
                   "args": lambda p: {"industry": p["industry"], "scenario": p["scenario"],
                                      "amount": p["amount"], "n_per_group": p["n_per_group"]}}],
    },
    {
        "intent": "risk.stress", "name": "压力测试",
        "keywords": ("压力", "冲击", "回款延迟", "融资", "成本上涨", "最坏"),
        "steps": [{"tool": "risk.stress_test", "desc": "四场景压力测试",
                   "args": lambda p: {"industry": p["industry"]}}],
    },
    {
        "intent": "tracking", "name": "三态追踪与到账验证",
        "keywords": ("三态", "到账", "应收", "追踪", "逾期", "化解", "观察"),
        "steps": [{"tool": "tracking.evaluate", "desc": "计算三态与到账验证覆盖率",
                   "args": lambda p: {"as_of": p.get("as_of")}},
                  {"tool": "tracking.timeline", "desc": "展示状态变化过程",
                   "args": lambda p: {"object_ref": "SYN-AR-001"}}],
    },
    {
        "intent": "clean", "name": "数据清洗与入库",
        "keywords": ("清洗", "入库", "批次", "异常条数", "卡住", "脏数据"),
        "steps": [{"tool": "clean.run_batch", "desc": "执行清洗批次并写入 MySQL",
                   "args": lambda p: {"source_ref": "synthetic_eight_sheet_v1"}}],
    },
    {
        "intent": "audit", "name": "留痕审计",
        "keywords": ("留痕", "审计", "日志", "变更", "谁改", "历史"),
        "steps": [{"tool": "audit.query", "desc": "查询留痕归档",
                   "args": lambda p: {"limit": 20}}],
    },
    {
        "intent": "scope", "name": "数据范围检查",
        "keywords": ("范围", "覆盖", "开关", "八表", "未覆盖", "能力"),
        "steps": [{"tool": "data.scope_check", "desc": "列出能力覆盖情况", "args": lambda p: {}}],
    },
]

DEFAULT_INTENT = {
    "intent": "risk.assess", "name": "三层风控评估（默认）",
    "steps": INTENTS[1]["steps"],
}


def detect(text: str):
    t = (text or "").lower()
    best, score = None, 0
    for it in INTENTS:
        s = sum(1 for k in it["keywords"] if k in t)
        if s > score:
            best, score = it, s
    return best or DEFAULT_INTENT, score


def build_plan(text: str, params: dict, known_tools: list = None) -> dict:
    """生成执行计划；known_tools 非空时校验工具存在性。"""
    params = P.normalize_params(params)
    it, score = detect(text)
    steps = []
    for s in it["steps"]:
        args = s["args"](params)
        if known_tools is not None and s["tool"] not in known_tools:
            continue
        steps.append({"id": "s%d" % (len(steps) + 1), "tool": s["tool"],
                      "desc": s["desc"], "args": args})
    if not steps:
        return {"ok": False, "error": "计划中没有可用工具（可能工具层未就绪）"}
    return {
        "ok": True, "intent": it["intent"], "intent_name": it["name"],
        "confidence": round(min(1.0, score / 3.0), 2),
        "steps": steps, "params": params,
        "agent": "local-planner",
        "reply": "已按「%s」编排 %d 个步骤：%s" % (
            it["name"], len(steps), " → ".join(s["tool"] for s in steps)),
    }


def validate_plan(plan: dict, known_tools: list) -> dict:
    """校验外部（千问）返回的计划：工具必须存在、参数必须是 dict。"""
    if not isinstance(plan, dict) or not plan.get("steps"):
        return {"ok": False, "error": "计划格式非法：缺少 steps"}
    bad = []
    for s in plan["steps"]:
        if s.get("tool") not in known_tools:
            bad.append(s.get("tool"))
        if not isinstance(s.get("args", {}), dict):
            bad.append("%s(args 非对象)" % s.get("tool"))
    if bad:
        return {"ok": False, "error": "计划引用了不存在的工具或参数非法：%s" % "、".join(map(str, bad))}
    return {"ok": True}


def explain(intent: str, result: dict, params: dict) -> str:
    """把结果解释成一句人话（千问不可用时由本地模板生成，不改写任何数字）。"""
    try:
        if intent == "risk.assess":
            fin = result.get("最终等级") or {}
            f = result.get("融合判定") or {}
            b = result.get("缓冲判定") or {}
            return ("三层融合：Q=%s / D=%s / P=%s → %s；缓冲判定 %s（倍数 %.2f）；"
                    "取较严格者 → %s（%s）。" % (
                        result["第一层"]["Q"], result["第二层"]["D"], result["第三层"]["P"],
                        f.get("等级"), b.get("等级"), float(b.get("缓冲倍数") or 0),
                        fin.get("等级"), fin.get("取严来源")))
        if intent == "cashflow.calc":
            a = (result.get("result") or {}).get("amounts") or {}
            return ("流入 %s / 流出 %s / 净额 %s / 期末 %s，对账 %s。" % (
                a.get("inflow"), a.get("outflow"), a.get("net"), a.get("closing"),
                (result.get("result") or {}).get("reconciliation_state")))
        if intent == "risk.stress":
            w = result.get("最坏情况") or {}
            return "压力测试最坏场景：%s，缓冲倍数 %.2f，等级 %s。" % (
                w.get("场景"), float(w.get("缓冲倍数") or 0), w.get("等级"))
        if intent == "tracking":
            recs = result.get("记录") or []
            return "；".join("%s → %s（%s，覆盖率 %.0f%%）" % (
                r.get("object_ref"), r.get("state"), r.get("lamp"),
                float(r.get("verified_ratio") or 0) * 100) for r in recs)
        if intent == "clean":
            return ("清洗批次 %s：总 %s 条，通过 %s 条，异常 %s 条，卡住=%s。" % (
                result.get("batch_id"), result.get("total_items"),
                result.get("passed_items"), result.get("anomaly_count"),
                result.get("blocked")))
        if intent == "audit":
            return "共查到 %s 条留痕记录。" % (result.get("total"))
    except Exception as e:
        return "（结果解释生成失败：%s）" % e
    return "已完成。"
