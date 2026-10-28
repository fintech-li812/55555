# -*- coding: utf-8 -*-
"""v8 风控工作台编排层：把三层模型、相对阈值、压力测试、审批、三态串成一次评估。

红线：
1. 现金流事实（期末现金、月均经营性流出、三类净额）来自已冻结的 v1.2 计算内核，真算；
2. 三层模型所需的 15 个财务比率为独立登记的合成演示输入，显著标注，不混入现金流结果；
3. 任何环节求解失败都如实标注降级，不伪装成功。
"""
from __future__ import annotations

import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from . import indicators as ind
from . import industry as ins
from . import layers
from . import fusion

# 压力测试输入：来自合成八表登记值（注明出处，非凭空假设）
STRESS_INPUTS = {
    "应收30天内到期余额": {"value": 70000.0,
                    "from": "应收账款表 SYN-AR-001：应收 20 万，已收 13 万，余 7 万未收"},
    "可用授信额度": {"value": 100000.0,
                "from": "筹资信息表：银行授信额度 10 万（非现金，仅作融资冲击基数）"},
    "月度主要成本": {"value": 70000.0,
                "from": "现金流水表 2026-08：经营性流出 7 万/月"},
}

DECLARATION = {
    "现金流来源": "v1.2 计算内核 program_workbench.calculate('synthetic_eight_sheet_v1')，真实计算",
    "财务比率来源": ind.DATA_NOTE,
    "不启用声明": "合成八表行业参数表注明“不启用评级或风险阈值”，故三层模型输入为独立登记的合成样本，与现金流结果分离",
}


def cashflow_facts():
    """取真实现金流事实；失败则如实返回 error，不伪造。"""
    try:
        import program_workbench as pw
        r = pw.calculate("synthetic_eight_sheet_v1")
        res = r.get("result") or {}
        am = res.get("amounts") or {}
        gr = res.get("cashflow_groups") or {}
        op_out = float((gr.get("operating") or {}).get("outflow") or 0)
        return {
            "ok": True,
            "期末现金": float(am.get("closing") or 0),
            "期初现金": float(am.get("opening") or 0),
            "本期净额": float(am.get("net") or 0),
            "月均经营性流出M": op_out,
            "经营性净额": float((gr.get("operating") or {}).get("net") or 0),
            "投资性净额": float((gr.get("investing") or {}).get("net") or 0),
            "筹资性净额": float((gr.get("financing") or {}).get("net") or 0),
            "对账": res.get("reconciliation_state"),
            "期间": res.get("period"),
            "run_id": res.get("run_id"),
        }
    except Exception as e:  # 如实报错
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def stress_test(industry: str, M: float, buffer: float):
    """常态化压力测试四场景（v8 4.3 节，相对缓冲倍数口径）。"""
    ar = STRESS_INPUTS["应收30天内到期余额"]["value"]
    credit = STRESS_INPUTS["可用授信额度"]["value"]
    cost = STRESS_INPUTS["月度主要成本"]["value"]
    scenes = [
        ("回款冲击", "所有应收账款延迟 30 天到账", ar),
        ("融资冲击", "授信额度缩减 50%", credit * 0.5),
        ("成本冲击", "主要成本上涨 20%", cost * 0.2),
        ("联合冲击", "以上三项同时发生", ar + credit * 0.5 + cost * 0.2),
    ]
    out = []
    for name, desc, gap in scenes:
        b = buffer - gap
        j = ins.judge_by_buffer(industry, M, b)
        days = (b / M * 30.0) if M > 0 else 0.0
        out.append({
            "场景": name, "说明": desc, "缺口": gap,
            "冲击后缓冲": b, "缓冲倍数": j["缓冲倍数"],
            "缓冲剩余天数": round(days, 1), "等级": j["等级"],
            "判定依据": j["判定依据"],
        })
    worst = min(out, key=lambda s: s["缓冲倍数"])
    return {
        "场景结果": out,
        "最坏情况": worst,
        "触发紧急任务": worst["等级"] == "红灯",
        "口径": "缓冲倍数 = 冲击后缓冲 ÷ 月均经营性流出 M；< K红 红灯，< K黄 黄灯",
        "输入出处": {k: v["from"] for k, v in STRESS_INPUTS.items()},
    }


def tracking_states():
    """三态跟踪 + 到账验证（v8 模块五）。"""
    return {
        "记录": [
            {"对象": "应收账款 SYN-AR-001（合成客户A）",
             "应收": 200000.0, "已收": 130000.0, "未收": 70000.0,
             "到账状态": "部分到账", "验证方式": "凭证上传 + 抽检 20%",
             "计入缓冲": "仅已验证到账部分计入", "状态": "观察中"},
            {"对象": "银行流水 SYN-T1（货款 13 万）",
             "应收": 130000.0, "已收": 130000.0, "未收": 0.0,
             "到账状态": "已到账", "验证方式": "银行流水自动比对",
             "计入缓冲": "计入", "状态": "已化解"},
        ],
        "规则": "待验证金额不计入现金缓冲；只有验证通过后才纳入缓冲计算",
        "三态": {"已化解": "绿色", "观察中": "黄色 + 倒计时", "已恶化": "红色 + 提醒"},
    }


def exception_review():
    """例外审批回溯（v8 模块四 4.2）。"""
    return {
        "部门统计": [
            {"部门": "采购部", "审批次数": 24, "例外次数": 6, "例外率": 0.25,
             "例外金额": 380000.0, "判定": "异常偏高（例外率 > 20%）", "动作": "触发审计检查"},
            {"部门": "销售部", "审批次数": 31, "例外次数": 3, "例外率": 0.097,
             "例外金额": 120000.0, "判定": "正常", "动作": "无"},
            {"部门": "生产部", "审批次数": 18, "例外次数": 1, "例外率": 0.056,
             "例外金额": 45000.0, "判定": "正常", "动作": "无"},
        ],
        "判定规则": ["例外率 > 0.2 → 异常偏高", "连续 3 次理由相同且长度 < 10 → 理由敷衍",
                 "例外金额 > 季度预算 × 0.15 → 金额异常"],
        "说明": "本表为 v8 设计的回溯演示数据（合成），用于展示机制而非真实审计结论",
    }


def assess(industry: str = "制造业", scenario: str = "edge",
           amount: float = 120000.0, n_per_group: int = 20):
    """一次完整 v8 评估。"""
    facts = cashflow_facts()
    prof = ins.get(industry)
    if not facts.get("ok"):
        M, buffer = 0.0, 0.0
    else:
        M = facts["月均经营性流出M"]
        buffer = facts["期末现金"]

    pool = ind.build_sample_pool(n_per_group)
    target = ind.TARGET_SCENARIOS.get(scenario) or ind.TARGET_SCENARIOS["edge"]

    gate = layers.quality_gate(pool)
    da = layers.deada(pool, target["values"])
    rf = layers.benford_rf(pool, target["values"], gate)
    # 上游可用性必须显式传入：DEA-DA 求解降级时 D 不可信，
    # 随机森林未训练出任何树时 P 无意义。不传就等同于默认「可信」，
    # 会把「无法判断」静默当成「无风险」（详见 fusion.py 模块说明）。
    fused = fusion.fuse(gate["Q"], da["D"], rf["P"],
                        d_reliable=not da.get("降级", False),
                        p_reliable=rf.get("树数", 0) > 0)

    buffer_judge = ins.judge_by_buffer(industry, M, buffer) if M else None
    thr = ins.warning_lines(industry, M) if M else None
    stress = stress_test(industry, M, buffer) if M else None
    appr = ins.approval_route(industry, amount,
                              M + STRESS_INPUTS["月度主要成本"]["value"] * 0.0 or M or 1.0)

    # v8 文档 4.0：缓冲阈值判定并未废弃，而是降级为第二层的辅助约束，
    # 两层逻辑并行，最终结果取较严格者。
    #
    # 取严的两个前提必须显式校验，不能用 dict.get(..., 0) 蒙混：
    #   1. 参与比较的等级必须是已知灯色——未知/缺失若按默认 0 处理，
    #      等于把「没算出来」当成「绿灯（最宽松）」，取严就失效了；
    #   2. 口径文案必须按实际参与比较的维度生成——缓冲维度缺失时
    #      仍宣称「两层并行取严」会让人误以为最终等级含缓冲因素。
    rank = {"绿灯": 0, "黄灯": 1, "红灯": 2}
    fused_level = fused["等级"]
    buffer_level = buffer_judge["等级"] if buffer_judge else None

    final_level = fused_level
    stricter = "三层融合判定 FUSE(Q,D,P)"
    if buffer_level in rank and rank[buffer_level] > rank.get(fused_level, 0):
        final_level = buffer_level
        stricter = "现金流缓冲判定（相对阈值 M×K）更严格"

    sources = ["三层融合判定 FUSE(Q,D,P)"]
    if buffer_level in rank:
        sources.append("现金流缓冲判定（相对阈值 M×K）")
    note = "取较严格者（参与维度：%s）" % "、".join(sources)
    if buffer_level not in rank:
        note += ("；缓冲维度未参与比较（现金流事实不可用或月均流出为 0），"
                 "最终等级仅由三层融合给出")

    final = {
        "等级": final_level, "取严来源": stricter,
        "三层融合": fused_level,
        "缓冲判定": buffer_level if buffer_level else "—",
        "口径": note,
        "缓冲维度参与比较": buffer_level in rank,
    }

    return {
        "schema": "mfg-v8-risk-assessment.v1",
        "行业": industry, "行业参数": prof,
        "受评对象": target["name"], "受评说明": target["note"],
        "声明": DECLARATION,
        "现金流事实": facts,
        "相对阈值": thr, "缓冲判定": buffer_judge,
        "第一层": gate, "第二层": da, "第三层": rf, "融合判定": fused, "最终等级": final,
        "压力测试": stress,
        "分级审批": appr,
        "三态跟踪": tracking_states(),
        "例外回溯": exception_review(),
    }
