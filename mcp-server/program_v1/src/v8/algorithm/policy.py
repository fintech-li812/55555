# -*- coding: utf-8 -*-
"""决策政策层：证据等级、灯色升级、动态阈值、多引擎统一视图。

为什么单独一层
--------------
算法层只出"数值 + 这个数值能不能用"；把数值变成灯色、把灯色变成动作，
是**政策**，不是算法。混在一起会让"改阈值"变成"改模型"，
也会让人误以为 0.7 是统计上推导出来的临界值。

本层的三条铁律
--------------
1. **只升不降**：证据不足时不降低严重度，只把绿灯升为黄灯。
   反例是把"P 证据不可用"当成"风险下降"，会让最危险的样本从红灯掉到黄灯。
2. **证据缺失不冒充结论**：全缺失时输出"待复核"，用词是"数据不足以支撑结论"，
   不是"未发现风险"。
3. **政策是候选**：动态阈值与统一视图都标注了"候选政策，未经业务批准不接入"。
"""
from __future__ import annotations

import logging
import random
from datetime import date

from .validation import ValidationError, float_param, int_param, is_number

LOGGER = logging.getLogger("v8.algorithm.policy")

POLICY_VERSION = "risk-policy-1.0"

LEVEL_ORDER = {"绿灯": 0, "黄灯": 1, "红灯": 2}
# 允许调用方传 0/1/2，避免字符串/整数混用造成 KeyError
_LEVEL_ALIASES = {0: "绿灯", 1: "黄灯", 2: "红灯", "绿灯": "绿灯",
                  "黄灯": "黄灯", "红灯": "红灯"}

# 证据等级：缺失 < 不足 < 充分
EVIDENCE_ORDER = {"缺失": 0, "不足": 1, "充分": 2}


def canonical_level(value):
    """把 0/1/2 或中文灯色归一成中文灯色；非法输入返回 None。"""
    return _LEVEL_ALIASES.get(value)


def quantile(values, q: float) -> float:
    """线性插值分位数。空序列直接报错——调用方必须先保证非空。"""
    a = sorted(values)
    if not a:
        raise ValidationError("分位数需要非空序列", field="values")
    p = (len(a) - 1) * q
    i = int(p)
    return a[i] + (a[min(i + 1, len(a) - 1)] - a[i]) * (p - i)


# ---------------------------------------------------------------- 证据等级


def evidence_levels(quality: dict, dead: dict, rf: dict, *,
                    benford_applicable: bool | None = None,
                    q_in_fusion: bool = True) -> dict:
    """把三层的可用性 / 可靠性折算成统一的证据等级。

    与 v0.3 的一处关键差异：**不可用时不再把该层从融合里摘掉**。
    P 数值照常参与融合表（保留它触发红灯的能力），
    只是额外标注"证据不足"。摘掉输入会让最危险的样本降档。

    ``q_in_fusion=False``（开关：Q 降为数据质量报告）：
    Q 的证据等级**照实记录**（不假装它充分），但通过 ``skipped`` 返回给调用方，
    由 ``overall_evidence`` 排除在短板计算之外——
    否则"让 Q 退出判别"与"Q 证据不足卡住绿灯"会互相抵消，开关开了等于没开。
    Q 的真实等级仍单独呈现在 ``证据.各层.Q`` 中供人工复核参考，只是不作为放行门槛。

    返回 (各层等级, 不参与短板计算的层集合)。
    """
    if not quality.get("available") or quality.get("Q") is None:
        q_level = "缺失"
    elif benford_applicable is False:
        q_level = "缺失"      # 显式声明不适用 → 该层不参与决策，等同于没有证据
    elif benford_applicable is None:
        q_level = "不足"      # Benford 对有界比率的适用性未声明
    else:
        q_level = "充分"

    if dead.get("D") is None:
        d_level = "缺失"
    elif dead.get("求解器") and "fallback" in dead.get("求解器", ""):
        d_level = "不足"      # 走的是备选路径，与历史锚点不是同一条 pivot 路径
    elif dead.get("最优性确认"):
        d_level = "充分"
    else:
        d_level = "不足"      # 数值可用但对偶最优性未确认

    if not rf.get("available") or rf.get("P") is None:
        p_level = "缺失"
    elif rf.get("校准", {}).get("样本充足") and rf.get("校准", {}).get("收敛"):
        p_level = "充分"
    else:
        p_level = "不足"

    skipped = frozenset(["Q"]) if (not q_in_fusion and q_level != "缺失") else frozenset()
    return {"Q": q_level, "D": d_level, "P": p_level}, skipped


def overall_evidence(levels: dict, skip: set | frozenset = frozenset()) -> str:
    """整体证据等级取最弱的一项（短板决定）。

    ``skip`` 中的层只参与记录、不参与短板计算（Q 退出判别开关用）。
    全部被跳过时按「缺失」处理——没有任何层背书时不得视为证据充分。
    """
    active = {k: v for k, v in levels.items() if k not in skip}
    if not active:
        return "缺失"
    return min(active.values(), key=lambda v: EVIDENCE_ORDER[v])


def apply_evidence(level: str, levels: dict, *,
                   require_full_evidence_for_green: bool = True,
                   skip: set | frozenset = frozenset()):
    """按证据等级调整灯色：**只升不降**。

    返回 (调整后灯色, 是否需人工复核, 说明)。
    """
    overall = overall_evidence(levels, skip)
    if overall == "充分":
        return level, False, "三层证据均充分"
    if not require_full_evidence_for_green:
        return level, True, "证据等级：%s（未启用绿灯升级）" % overall

    notes = []
    for name in ("Q", "D", "P"):
        if levels[name] == "充分":
            continue
        if name in skip:
            notes.append("%s 证据%s（仅报告，不作门槛）" % (name, levels[name]))
        else:
            notes.append("%s 证据%s" % (name, levels[name]))
    reason = "；".join(notes) or "各层均无充分证据"
    if overall == "缺失":
        return level, True, "证据缺失（%s）：数据不足以支撑结论，需人工复核" % reason
    if canonical_level(level) == "绿灯":
        return "黄灯", True, "证据不足（%s）：不得放行绿灯，已升为黄灯" % reason
    return level, True, "证据不足（%s）：严重度不作下调" % reason


# ---------------------------------------------------------------- 动态阈值（候选政策）


def threshold_policy(history, static_yellow, static_red, as_of, *, window: int = 12,
                     bootstrap: int = 199, seed: int = 20261029):
    """现金缓冲阈值的动态启用政策（**候选**，未接入线上）。

    设计要点
    --------
    * 必须拿到「连续、无重复、严格早于评估月」的最近 window 个月；
      任何一条不满足就整体回退到静态阈值，**不静默丢行**。
    * 最终阈值 = max(静态阈值, 分位数)。静态下限是刻意的设计：
      否则一家长期低缓冲的企业，其阈值会跟着自己越降越低，
      等于把慢性恶化归一化成"正常"。
    * 分位数区间用 3 个月循环块 bootstrap，属探索性，不保证覆盖率。
    """
    static_yellow = float_param(static_yellow, "static_yellow", 0.0, 1e12)
    static_red = float_param(static_red, "static_red", 0.0, 1e12)
    if static_red >= static_yellow:
        raise ValidationError("要求 0 ≤ 静态红线 < 静态黄线", field="static_red")
    window = int_param(window, "window", 12, 240)
    bootstrap = int_param(bootstrap, "bootstrap", 99, 5000)

    result = {"enabled": False, "mode": "static", "yellow": static_yellow,
              "red": static_red, "window": window,
              "policy": "static_floor", "policy_version": POLICY_VERSION}

    def fallback(reason: str):
        LOGGER.info("threshold_policy 回退静态：%s", reason)
        return dict(result, reason=reason)

    if not isinstance(history, (list, tuple)):
        return fallback("历史必须是月度序列")
    rows = []
    for item in history:
        if not isinstance(item, dict):
            return fallback("历史项必须是 dict（month + ratio）")
        month = item.get("month")
        ratio = item.get("ratio")
        if not isinstance(month, str):
            return fallback("month 必须是 'YYYY-MM' 字符串")
        try:
            d = date.fromisoformat(month + "-01")
        except ValueError:
            return fallback("month 非法：%s" % month)
        if d.strftime("%Y-%m") != month:
            return fallback("month 非法：%s" % month)
        if not is_number(ratio) or ratio < 0:
            return fallback("ratio 必须是非负有限数值")
        rows.append((d, float(ratio)))
    if not rows:
        return fallback("历史为空")

    try:
        cutoff = date.fromisoformat(as_of + "-01")
    except (ValueError, TypeError, AttributeError):
        return fallback("as_of 必须是 'YYYY-MM'")
    if any(d >= cutoff for d, _ in rows):
        return fallback("拒绝当前月或未来月记录（防止前瞻）")
    if len({d for d, _ in rows}) != len(rows):
        return fallback("月份重复")
    rows.sort()
    if len(rows) < window:
        return fallback("月度历史不足 %d 个月（当前 %d）" % (window, len(rows)))
    rows = rows[-window:]
    idx = [d.year * 12 + d.month for d, _ in rows]
    if idx[-1] != cutoff.year * 12 + cutoff.month - 1:
        return fallback("历史必须结束于评估月的上一个月")
    if any(bb - aa != 1 for aa, bb in zip(idx, idx[1:])):
        return fallback("历史月份必须连续")

    values = [v for _, v in rows]
    q_yellow = quantile(values, 0.25)
    q_red = quantile(values, 0.10)
    if q_yellow <= q_red:
        return fallback("分位数退化（P25 ≤ P10），回退静态")

    rng = random.Random(seed)
    draws = []
    block = 3
    for _ in range(bootstrap):
        sample = []
        while len(sample) < window:
            start = rng.randrange(window)
            sample.extend(values[(start + j) % window] for j in range(block))
        draws.append((quantile(sample[:window], 0.10), quantile(sample[:window], 0.25)))
    intervals = {
        "red": [quantile([d[0] for d in draws], 0.025),
                quantile([d[0] for d in draws], 0.975)],
        "yellow": [quantile([d[1] for d in draws], 0.025),
                   quantile([d[1] for d in draws], 0.975)],
    }
    return dict(
        result,
        enabled=True,
        mode="dynamic_with_static_floor",
        yellow=max(static_yellow, q_yellow),
        red=max(static_red, q_red),
        raw_quantiles={"yellow": q_yellow, "red": q_red},
        confidence={
            "type": "探索性 3 个月循环块 bootstrap 分位区间",
            "intervals": intervals,
            "coverage_guaranteed": False,
            "bootstrap": bootstrap,
            "note": "区间针对原始分位数，不针对取静态下限后的决策阈值",
        },
        reason="滞后连续历史校验通过；静态下限防止把慢性低缓冲归一化",
    )


# ---------------------------------------------------------------- 多引擎统一视图（候选政策）


def unified_view(cross_section, buffer_level, temporal_probability, *,
                 temporal_validated: bool = False, yellow_probability: float = 0.1,
                 red_probability: float = 0.3):
    """截面 / 现金缓冲 / 时间缺口概率三个引擎的合成视图（**候选**）。

    这是**等级决策规则**，没有联合概率解释：三路不加权、不平均、
    不做概率换算，取已知维度中最严重的一档。
    缺任何一维时不得放行绿灯；全部缺失则为"待复核"。
    """
    yellow_probability = float_param(yellow_probability, "yellow_probability", 0.0, 1.0)
    red_probability = float_param(red_probability, "red_probability", 0.0, 1.0)
    if not 0 <= yellow_probability < red_probability <= 1:
        raise ValidationError("要求 0 ≤ 黄线概率 < 红线概率 ≤ 1", field="red_probability")

    components, missing = {}, []
    for name, level in (("cross_section", cross_section), ("buffer", buffer_level)):
        canon = canonical_level(level)
        if canon is None:
            missing.append(name)
        else:
            components[name] = canon
    temporal = None
    if is_number(temporal_probability) and 0 <= temporal_probability <= 1 and temporal_validated:
        temporal = ("红灯" if temporal_probability >= red_probability
                    else "黄灯" if temporal_probability >= yellow_probability else "绿灯")
        components["temporal"] = temporal
    else:
        missing.append("temporal")

    if not components:
        return {"level": "待复核", "complete": False, "missing": missing,
                "components": {}, "policy_version": POLICY_VERSION,
                "reason": "三个引擎均无有效结论，无法给出等级"}

    level = max(components.values(), key=lambda v: LEVEL_ORDER[v])
    if missing and level == "绿灯":
        level = "黄灯"
    return {
        "level": level,
        "complete": not missing,
        "missing": missing,
        "components": components,
        "policy_version": POLICY_VERSION,
        "reason": "取已知维度中最严重一档；缺证据不得放行绿灯",
        "probability_combined": False,
        "business_approval": "候选政策，未部署",
    }
