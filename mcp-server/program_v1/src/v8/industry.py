# -*- coding: utf-8 -*-
"""v8 四大行业参数模板与相对指标阈值。

口径来自 docs/v8_完整方案.md 第五章「四大行业参数模板与阈值来源表」：
所有预警线 = 月均经营性流出 M × 行业系数 K，不再使用绝对金额。
"""
from __future__ import annotations

import math

# 阈值来源（v8 5.0 节）：出处=上市公司年报/中国统计年鉴，年份=2024，样本量见备注。
# 该来源为方案预设值，方案本身要求"参赛前重新核对最新年报并标注页码"，此处如实保留。
THRESHOLD_SOURCE = {
    "book": "上市公司年报 / 中国统计年鉴",
    "year": "2024",
    "note": "方案预设值，非本工程实测；K黄取样本 P25 分位，K红取 P10 分位。",
}

INDUSTRIES = {
    "制造业": {
        "code": "C13",
        "K黄": 2.5, "K红": 1.5,
        "时间缓冲黄灯天": 45, "时间缓冲红灯天": 25,
        "融资缓冲权重": 0.40, "经营调整缓冲权重": 0.30,
        "应收账款周转天数比": 1.3, "存货周转天数比": 1.3,
        "流动比率比": 0.7, "资产负债率警戒": 0.70,
        "审批分界线1": 0.5, "审批分界线2": 2.0,
        "特殊规则": ["存货周转天数/行业均值 > 1.3 标记运营风险",
                 "原材料价格波动 > 20% 触发成本冲击模拟"],
    },
    "零售业": {
        "code": "F52",
        "K黄": 1.8, "K红": 1.0,
        "时间缓冲黄灯天": 30, "时间缓冲红灯天": 15,
        "融资缓冲权重": 0.20, "经营调整缓冲权重": 0.50,
        "应收账款周转天数比": 1.5, "存货周转天数比": 1.3,
        "流动比率比": 0.7, "资产负债率警戒": 0.65,
        "审批分界线1": 0.3, "审批分界线2": 1.0,
        "特殊规则": ["春节/双十一 K黄 上浮至 2.3、K红 上浮至 1.3",
                 "库存积压 > 60 天触发折价清仓推演"],
    },
    "建筑业": {
        "code": "E48",
        "K黄": 3.5, "K红": 2.0,
        "时间缓冲黄灯天": 60, "时间缓冲红灯天": 30,
        "融资缓冲权重": 0.50, "经营调整缓冲权重": 0.20,
        "应收账款周转天数比": 1.5, "存货周转天数比": 1.5,
        "流动比率比": 0.8, "资产负债率警戒": 0.80,
        "审批分界线1": 1.0, "审批分界线2": 4.0,
        "特殊规则": ["完工百分比/付款比例 < 0.8 触发预警",
                 "甲方付款逾期 > 30 天自动升级风险等级"],
    },
    "科技服务业": {
        "code": "M75",
        "K黄": 1.5, "K红": 1.0,
        "时间缓冲黄灯天": 20, "时间缓冲红灯天": 10,
        "融资缓冲权重": 0.15, "经营调整缓冲权重": 0.55,
        "应收账款周转天数比": 1.3, "存货周转天数比": 1.5,
        "流动比率比": 0.8, "资产负债率警戒": 0.55,
        "审批分界线1": 0.3, "审批分界线2": 1.0,
        "特殊规则": ["人力成本/月均业务规模 > 0.7 触发人力结构推演",
                 "项目毛利率 < 20% 标记盈利风险"],
    },
}

DEFAULT_INDUSTRY = "制造业"

# 动态阈值启用条件（差距报告 v7 遗留 #8：统一为「数据满 12 个月」）
DYNAMIC_MIN_MONTHS = 12
# 分位数口径与静态阈值保持一致：K黄=P25、K红=P10（见 THRESHOLD_SOURCE.note）
DYNAMIC_Q_YELLOW = 0.25
DYNAMIC_Q_RED = 0.10


def get(industry: str) -> dict:
    return INDUSTRIES.get(industry) or INDUSTRIES[DEFAULT_INDUSTRY]


def percentile(values: list, q: float) -> float:
    """线性插值分位数，与 numpy.percentile(method="linear") 口径一致。

    单独实现而不用 numpy/statistics，是为了避免引入新依赖，并且让「同样的输入
    在任何环境下得到同样的输出」——浮点插值是确定性的，不涉及任何平台相关的
    库实现差异（这一点对可复现性很重要，参见 layers._simplex 的修复说明）。
    """
    if not values:
        raise ValueError("percentile: 输入序列为空")
    if not 0.0 <= q <= 1.0:
        raise ValueError("percentile: q 必须落在 [0,1]，收到 %r" % q)
    s = sorted(float(v) for v in values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def dynamic_thresholds(industry: str, history: list | None) -> dict:
    """按企业自身历史缓冲倍数求动态阈值（滚动分位数）。

    对应差距报告 v7 遗留第 8 条「动态阈值启用条件统一为数据满 12 个月」与
    第十八章「风控闸（动态）：表格 + Agent，≥12 个月」。

    参数
    ----
    history : 按月的历史「缓冲倍数」序列（缓冲 ÷ 月均经营性流出 M），
              按时间从旧到新排列。长度不足 DYNAMIC_MIN_MONTHS 时回退静态阈值。

    返回
    ----
    始终包含 启用/模式/K黄/K红/样本月数/口径 六个键；无论启用与否，都给出
    静态对照值，便于前端解释「为什么这次用动态 / 为什么还在用静态」。

    不静默降级：历史不足时明确标注 mode=static 与原因，不假装已动态化。
    """
    p = get(industry)
    static = {"K黄": p["K黄"], "K红": p["K红"]}
    hist = [float(h) for h in (history or []) if h is not None]
    n = len(hist)

    if n < DYNAMIC_MIN_MONTHS:
        return {
            "启用": False, "模式": "static",
            "K黄": static["K黄"], "K红": static["K红"],
            "样本月数": n, "最低月数": DYNAMIC_MIN_MONTHS,
            "原因": "历史缓冲倍数仅 %d 个月，未满 %d 个月，保持静态阈值"
                    % (n, DYNAMIC_MIN_MONTHS),
            "静态对照": static,
            "口径": "动态阈值启用条件：连续 %d 个月历史缓冲倍数" % DYNAMIC_MIN_MONTHS,
        }

    ky = percentile(hist, DYNAMIC_Q_YELLOW)
    kr = percentile(hist, DYNAMIC_Q_RED)
    # K红 必须不高于 K黄，否则判定逻辑（<K红 红灯 / <K黄 黄灯）会出现空红灯区。
    # 极端样本（如大量月份缓冲倍数为 0）会让两者相等甚至倒挂，此处如实修正并标注。
    inverted = kr > ky
    if inverted:
        kr, ky = ky, kr
    return {
        "启用": True, "模式": "dynamic",
        "K黄": round(ky, 4), "K红": round(kr, 4),
        "样本月数": n, "最低月数": DYNAMIC_MIN_MONTHS,
        "原因": "历史缓冲倍数满 %d 个月，启用滚动分位数阈值" % n,
        "静态对照": static,
        "与静态差异": {
            "K黄": round(ky - static["K黄"], 4),
            "K红": round(kr - static["K红"], 4),
            "更严格": (ky > static["K黄"]) or (kr > static["K红"]),
        },
        "口径": "K黄 = 历史缓冲倍数 P%.0f，K红 = P%.0f（与静态阈值同口径，便于对照）"
                % (DYNAMIC_Q_YELLOW * 100, DYNAMIC_Q_RED * 100),
        "分位区间": {"min": round(min(hist), 4), "max": round(max(hist), 4)},
        "已修正倒挂": inverted,
    }


def warning_lines(industry: str, M: float, history: list | None = None) -> dict:
    """相对阈值：预警线 = 月均经营性流出 M × 系数 K。

    history 给出近 N 个月的历史缓冲倍数时，优先用动态阈值；
    历史不足 12 个月则自动回退静态阈值，并在返回里写明原因（不静默降级）。
    """
    dyn = dynamic_thresholds(industry, history)
    return {
        "M_月均经营性流出": M,
        "K黄": dyn["K黄"], "K红": dyn["K红"],
        "黄灯线": M * dyn["K黄"], "红灯线": M * dyn["K红"],
        "动态阈值": dyn,
        "口径": "现金缓冲预警线 = M × K（相对指标，随企业规模自动缩放）",
    }


def judge_by_buffer(industry: str, M: float, buffer: float,
                    history: list | None = None) -> dict:
    """按缓冲倍数判定（v8 相对口径）：实际缓冲 ÷ M 与 K红/K黄 比较。

    history 透传给 warning_lines，用于启用动态阈值。
    """
    w = warning_lines(industry, M, history)
    ratio = (buffer / M) if M else 0.0
    if ratio < w["K红"]:
        level, why = "红灯", "缓冲倍数 %.2f < K红 %.2f" % (ratio, w["K红"])
    elif ratio < w["K黄"]:
        level, why = "黄灯", "缓冲倍数 %.2f 介于 K红 %.2f 与 K黄 %.2f 之间" % (ratio, w["K红"], w["K黄"])
    else:
        level, why = "绿灯", "缓冲倍数 %.2f ≥ K黄 %.2f" % (ratio, w["K黄"])
    return {"缓冲": buffer, "缓冲倍数": ratio, "等级": level, "判定依据": why, "阈值": w}


def approval_route(industry: str, amount: float, monthly_business_scale: float) -> dict:
    """分级审批（v8 相对口径）：分界线 = 月均业务规模 × 比例。"""
    p = get(industry)
    s = monthly_business_scale if monthly_business_scale else 0.0
    r = (amount / s) if s else 0.0
    if r <= p["审批分界线1"]:
        who, need = "部门经理", "常规审批"
    elif r <= p["审批分界线2"]:
        who, need = "财务总监", "财务总监签字"
    else:
        who, need = "双签", "财务总监 + 总经理"
    return {
        "金额": amount, "月均业务规模": s, "金额倍数": r,
        "审批人": who, "要求": need,
        "分界线": {"线1": p["审批分界线1"], "线2": p["审批分界线2"]},
        "口径": "审批分界线 = 月均业务规模 × 比例（相对指标）",
    }
