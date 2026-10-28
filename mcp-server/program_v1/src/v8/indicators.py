# -*- coding: utf-8 -*-
"""v8 三层风控模型所需的财务比率输入。

⚠️ 数据来源声明（必须如实展示，不得省略）：
现有合成八表的「行业参数」表明确写着"合成演示不启用评级或风险阈值"，
「企业基本信息」里缓冲=“未启用风险模型”。也就是说，15 个财务比率在八表中
并不存在。本文件提供的是**独立登记的合成演示输入**，用于驱动三层风控模型，
它与已冻结的 v1.2 现金流计算结果是分离的、不混入的。

所有数值由固定随机种子生成，可复现；不代表任何真实企业。
"""
from __future__ import annotations

import math
import random

# 15 个财务比率（v8 文档 2-A.4 / 2-A.5 涉及的 X1~X15）
# nature: positive=正指标（越大越好）；inverse=逆指标（越小越好）
INDICATORS = [
    ("X1", "净资产收益率", "positive"),
    ("X2", "资产报酬率", "positive"),
    ("X3", "营业利润率", "positive"),
    ("X4", "流动比率", "positive"),
    ("X5", "速动比率", "positive"),
    ("X6", "利息保障倍数", "positive"),
    ("X7", "利润总额增长率", "positive"),
    ("X8", "营业收入增长率", "positive"),
    ("X9", "总资产增长率", "positive"),
    ("X10", "应收账款周转率", "positive"),
    ("X11", "固定资产周转率", "positive"),
    ("X12", "总资产周转率", "positive"),
    ("X13", "资产负债率", "inverse"),
    ("X14", "成本费用利润率", "positive"),
    ("X15", "总资产现金回收率", "positive"),
]

KEYS = [k for k, _, _ in INDICATORS]
NAME = {k: n for k, n, _ in INDICATORS}
NATURE = {k: t for k, _, t in INDICATORS}

# 决策树筛选出的 7 个核心指标（v8 文档 2-A.5 Step 1）
CORE7 = ["X3", "X6", "X2", "X1", "X9", "X7", "X5"]

# DEA-DA 投入产出配对（v8 文档 2-A.5 Step 3）
# 投入：资产扩张、利润增长、资金占用（应收周转天数）、财务杠杆
# 产出：股东回报、资产效率、盈利效率、偿债能力
DEADA_INPUTS = ["X9", "X7", "AR_DAYS", "X13"]
DEADA_OUTPUTS = ["X1", "X2", "X3", "X6"]

SEED = 20261027
DATA_NOTE = "合成演示输入（固定种子 %d 生成，可复现）；不代表任何真实企业" % SEED


# Benford 理论频率 f_B(d) = log10(1 + 1/d)
BENFORD = [math.log10(1.0 + 1.0 / d) for d in range(1, 10)]

# 各指标取值区间：(健康组, 风险组)。区间跨越多个首位数字，使 Benford 检验有意义。
RANGES = {
    "X1": ((0.01, 0.45), (-0.28, 0.08)),
    "X2": ((0.005, 0.30), (-0.18, 0.06)),
    "X3": ((0.01, 0.38), (-0.22, 0.06)),
    "X4": ((0.60, 4.80), (0.35, 1.60)),
    "X5": ((0.40, 3.40), (0.25, 1.10)),
    "X6": ((0.80, 14.0), (0.30, 2.60)),
    "X7": ((0.01, 0.90), (-0.55, 0.05)),
    "X8": ((0.01, 0.80), (-0.45, 0.06)),
    "X9": ((0.01, 0.60), (-0.25, 0.08)),
    "X10": ((1.50, 19.0), (1.20, 7.50)),
    "X11": ((0.50, 9.00), (0.40, 3.00)),
    "X12": ((0.12, 2.20), (0.15, 0.90)),
    "X13": ((0.25, 0.72), (0.50, 0.95)),
    "X14": ((0.01, 0.50), (-0.25, 0.06)),
    "X15": ((0.01, 0.28), (-0.12, 0.05)),
}


def _digit_ranges(lo: float, hi: float) -> dict:
    """在 [lo, hi] 内首位数字 d 可取到的区间集合。"""
    out = {}
    if hi <= lo:
        return out
    e = int(math.floor(math.log10(lo))) if lo > 0 else -3
    while e <= 6:
        base = 10.0 ** e
        if base > hi:
            break
        for d in range(1, 10):
            a2, b2 = max(d * base, lo), min((d + 1) * base, hi)
            if b2 > a2:
                out.setdefault(d, []).append((a2, b2))
        e += 1
    return out


def _benford_magnitude(rng: random.Random, lo: float, hi: float) -> float:
    """按 Benford 概率加权选首位数字，再在该数字的区间内均匀取值。"""
    ranges = _digit_ranges(lo, hi)
    if not ranges:
        return rng.uniform(lo, hi)
    ws = [(d, BENFORD[d - 1]) for d in sorted(ranges)]
    tot = sum(w for _, w in ws) or 1.0
    r = rng.random() * tot
    acc, pick = 0.0, ws[-1][0]
    for d, w in ws:
        acc += w
        if r <= acc:
            pick = d
            break
    segs = ranges[pick]
    lens = [b - a for a, b in segs]
    tl = sum(lens) or 1.0
    r2 = rng.random() * tl
    acc2 = 0.0
    for (a, b), L in zip(segs, lens):
        acc2 += L
        if r2 <= acc2:
            return rng.uniform(a, b)
    return rng.uniform(*segs[-1])


def _tamper(rng: random.Random, x: float) -> float:
    """粉饰痕迹：末位凑整到 0/5，或把首位数字改成 1 或 5。"""
    if rng.random() < 0.55:
        x = round(x * 20.0) / 20.0       # 末位落在 0 或 5
    if rng.random() < 0.35 and x != 0:
        a = abs(x)
        e = int(math.floor(math.log10(a)))
        base = 10.0 ** e
        d = 1 if rng.random() < 0.6 else 5
        x = math.copysign((d + rng.random()) * base, x)
    return x


# 仅对少数指标施加粉饰痕迹（对应杨贵军等 2022 实证中"部分指标命中质量因子"的结论：
# Benford 命中 X4/X5/X7/X11/X12，Myer 命中 X2/X5/X7/X15，其余指标合规）
TAMPER_KEYS = ("X2", "X4", "X5", "X7", "X11", "X12", "X15")


def _base_values(rng: random.Random, healthy: bool) -> dict:
    """生成一组 15 个指标的合成取值。

    健康组：按 Benford 概率生成首位数字 → χ² 检验合规、末位自然分布 → Myer 合规。
    风险组：除取值更差外，额外施加粉饰痕迹（末位凑整、首位数字偏好 1/5），
           使第一层质量门禁能检出差异——这正是 BM-BP 双质量因子的作用点。
    """
    v = {}
    for k in KEYS:
        h, r = RANGES[k]
        lo, hi = h if healthy else r
        if lo >= 0:
            x = _benford_magnitude(rng, lo, hi)
        else:
            mag_hi = max(abs(lo), abs(hi))
            mag = _benford_magnitude(rng, mag_hi * 0.02, mag_hi)
            x = -mag if rng.random() < 0.7 else mag
            x = max(lo, min(hi, x))
        if not healthy and k in TAMPER_KEYS:
            x = _tamper(rng, x)
        v[k] = round(x, 6)
    return v


def build_sample_pool(n_per_group: int = 20) -> list:
    """构造合成样本库：用于 DEA-DA 分组判别与随机森林训练。"""
    rng = random.Random(SEED)
    pool = []
    for i in range(n_per_group):
        pool.append({"code": "H%02d" % (i + 1), "label": 0,
                     "values": _base_values(rng, True)})
    for i in range(n_per_group):
        pool.append({"code": "R%02d" % (i + 1), "label": 1,
                     "values": _base_values(rng, False)})
    return pool


# 三个受评企业场景（合成），用于演示三层模型在不同情形下的判定差异
TARGET_SCENARIOS = {
    "healthy": {
        "name": "受评企业A（经营稳健）",
        "note": "合成受评样本，各项指标处于健康区间",
        "values": {
            "X1": 0.156, "X2": 0.098, "X3": 0.121, "X4": 2.10,
            "X5": 1.45, "X6": 6.20, "X7": 0.184, "X8": 0.152,
            "X9": 0.126, "X10": 9.80, "X11": 4.10, "X12": 1.05,
            "X13": 0.42, "X14": 0.168, "X15": 0.104,
        },
    },
    "edge": {
        "name": "受评企业B（缓冲偏紧）",
        "note": "合成受评样本，盈利与偿债指标接近临界",
        "values": {
            "X1": 0.035, "X2": 0.021, "X3": 0.028, "X4": 1.28,
            "X5": 0.86, "X6": 2.05, "X7": 0.012, "X8": 0.008,
            "X9": 0.035, "X10": 5.60, "X11": 2.10, "X12": 0.62,
            "X13": 0.61, "X14": 0.042, "X15": 0.021,
        },
    },
    "risk": {
        "name": "受评企业C（风险+疑似粉饰）",
        "note": "合成受评样本，多项指标异常且末位数字存在堆积",
        "values": {
            "X1": -0.112, "X2": -0.068, "X3": -0.085, "X4": 0.82,
            "X5": 0.52, "X6": 0.85, "X7": -0.246, "X8": -0.192,
            "X9": -0.045, "X10": 3.10, "X11": 1.05, "X12": 0.34,
            "X13": 0.79, "X14": -0.062, "X15": -0.031,
        },
    },
}


def ar_days(values: dict) -> float:
    """应收账款周转天数 = 365 / 应收账款周转率（资金占用投入）。"""
    r = values.get("X10") or 0.0
    return (365.0 / r) if r > 0 else 999.0
