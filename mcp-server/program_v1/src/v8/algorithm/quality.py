# -*- coding: utf-8 -*-
"""第一层：数字分布质量门禁 Q。

做法：对 15 个财务比率各做 2 项数字分布检验（首位数字 / 末位数字），
共 30 项；用 **多项分布 Monte Carlo** 取精确 p 值（不是 χ² 渐近近似），
再用 **Holm 逐步法**在任意相关结构下控制族错误率，
最后 ``Q = 1 − 拒绝数 / 30``。

三条必须一起读的边界
--------------------
1. **Q 不是舞弊概率**，只表示"数字分布未发现异常"。
2. **Q 描述的是样本池，不是受评企业**：它只吃 pool，换一个受评对象
   Q 不变。想判断"这家企业是否造假"，需要对其原始金额序列做数字分析，
   那是另一套输入，本层做不到。
3. **对有界比率做 Benford 检验的适用性未被证明**。因此 Q 默认按
   "信息"输出，是否参与融合由调用方显式声明（见 policy.py 的证据门）。

本层只出结论，不做口径裁决。
"""
from __future__ import annotations

import logging
import math
import random
from bisect import bisect_left
from functools import lru_cache

from .. import indicators as ind
from .validation import (MIN_DIGITS, SIMULATIONS_RANGE, ValidationError,
                         float_param, int_param, is_number)

LOGGER = logging.getLogger("v8.algorithm.quality")

MC_SEED = 20261029
MC_SIMULATIONS = 1999
DEFAULT_ALPHA = 0.05

BENFORD = [math.log10(1.0 + 1.0 / d) for d in range(1, 10)]
UNIFORM10 = [0.1] * 10
# Wilson 区间用的 97.5% 正态分位点
Z975 = 1.959963984540054


def first_digit(x) -> int | None:
    """首位有效数字（1~9）。0 与非有限值返回 None（调用方须计数，不可当 0 用）。"""
    if not is_number(x):
        return None
    v = abs(float(x))
    if v == 0:
        return None
    while v >= 10:
        v /= 10.0
    while v < 1:
        v *= 10.0
    d = int(math.floor(v))
    return d if 1 <= d <= 9 else None


def last_digit(x) -> int | None:
    """末位数字（0~9），按六位小数格式化后去尾零取最后一位。"""
    if not is_number(x):
        return None
    s = "%.6f" % abs(float(x))
    s = s.rstrip("0")
    for ch in reversed(s):
        if ch.isdigit():
            return int(ch)
    return None


@lru_cache(maxsize=64)
def _null_distribution(n: int, probabilities: tuple, simulations: int, seed: int) -> tuple:
    """零假设下检验统计量的经验分布（升序）。

    15 个指标共用两组理论频率（Benford 9 格 / 均匀 10 格），
    因此每个样本量只算 2 次；这是 1999 次模拟仍能落在百毫秒内的原因。
    """
    rng = random.Random(seed)
    cumulative = []
    running = 0.0
    for p in probabilities:
        running += p
        cumulative.append(running)
    last = len(probabilities) - 1
    out = []
    for _ in range(simulations):
        counts = [0] * len(probabilities)
        for _ in range(n):
            counts[min(bisect_left(cumulative, rng.random()), last)] += 1
        out.append(sum((c - n * p) ** 2 / (n * p) for c, p in zip(counts, probabilities)))
    return tuple(sorted(out))


def mc_test(counts, probabilities, simulations: int = MC_SIMULATIONS,
            seed: int = MC_SEED) -> dict:
    """多项分布拟合优度的 Monte Carlo 检验。

    p 值用 ``(命中数 + 1) / (模拟次数 + 1)``，避免 p=0 造成的"绝对显著"假象。
    mc_区间 是 Wilson 区间，只量化**模拟次数**引入的不确定性，
    **不是**企业真实性的置信区间。
    """
    if not isinstance(counts, (list, tuple)) or not isinstance(probabilities, (list, tuple)):
        return {"available": False, "reason": "counts / probabilities 必须是序列"}
    if len(counts) != len(probabilities):
        return {"available": False, "reason": "counts 与 probabilities 长度不一致"}
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 0 for c in counts):
        return {"available": False, "reason": "计数必须是非负整数"}
    if any(not is_number(p) or p <= 0 for p in probabilities):
        return {"available": False, "reason": "理论频率必须为正且有限"}
    if abs(sum(probabilities) - 1.0) > 1e-10:
        return {"available": False, "reason": "理论频率之和须为 1"}
    n = int(sum(counts))
    if n < MIN_DIGITS:
        return {"available": False, "reason": "可用数字不足 %d 个（当前 %d）" % (MIN_DIGITS, n),
                "n": n}
    if not (SIMULATIONS_RANGE[0] <= simulations <= SIMULATIONS_RANGE[1]):
        return {"available": False, "reason": "模拟次数越界"}

    probs = tuple(float(p) for p in probabilities)
    stat = sum((c - n * p) ** 2 / (n * p) for c, p in zip(counts, probs))
    null = _null_distribution(n, probs, simulations, seed)
    hits = len(null) - bisect_left(null, stat - 1e-12)
    b = float(len(null))
    ph = hits / b
    den = 1 + Z975 * Z975 / b
    center = (ph + Z975 * Z975 / (2 * b)) / den
    width = (Z975 * math.sqrt(ph * (1 - ph) / b + Z975 * Z975 / (4 * b * b))) / den
    return {
        "available": True,
        "p": (hits + 1) / (b + 1),
        "统计量": stat,
        "n": n,
        "模拟次数": simulations,
        "mc_区间": [max(0.0, center - width), min(1.0, center + width)],
    }


def holm_reject(p_values, alpha: float) -> int:
    """Holm 逐步拒绝法，返回被拒绝的检验数。

    逐步法一旦遇到不拒绝就停止，因此**不假设各检验相互独立**——
    15 个财务比率显然高度相关，用 Bonferroni 会更保守、用独立假设则不成立。
    """
    ordered = sorted(p_values)
    rejected = 0
    for rank, p in enumerate(ordered):
        if p <= alpha / (len(ordered) - rank):
            rejected += 1
        else:
            break
    return rejected


def quality_gate(pool, *, alpha: float = DEFAULT_ALPHA,
                 simulations: int = MC_SIMULATIONS, seed: int = MC_SEED) -> dict:
    """第一层门禁，返回体**恒定带 Q 键**（不可用时为 None）。

    恒定带键是为了避免调用方在异常分支上 KeyError——
    这是上一版实测踩过的坑。
    """
    def fail(reason: str, **extra) -> dict:
        out = {"Q": None, "available": False, "reason": reason}
        out.update(extra)
        LOGGER.info("quality_gate 不可用：%s", reason)
        return out

    try:
        alpha = float_param(alpha, "alpha", 0.0, 1.0, lo_open=True, hi_open=True)
        simulations = int_param(simulations, "simulations", *SIMULATIONS_RANGE)
    except ValidationError as exc:
        raise exc

    if not isinstance(pool, (list, tuple)) or not pool:
        return fail("样本池为空")

    for s in pool:
        if not isinstance(s, dict) or not isinstance(s.get("values"), dict):
            return fail("样本结构非法：缺少 values")

    details = {}
    tests = []
    ignored = 0
    for key in ind.KEYS:
        vals = []
        for s in pool:
            v = s["values"].get(key)
            if not is_number(v):
                return fail("指标 %s 存在非数值或非有限值" % key)
            vals.append(float(v))
        firsts = [first_digit(v) for v in vals]
        lasts = [last_digit(v) for v in vals]
        ignored += sum(1 for d in firsts if d is None)
        t_first = mc_test([firsts.count(d) for d in range(1, 10)], BENFORD,
                          simulations, seed)
        t_last = mc_test([lasts.count(d) for d in range(10)], UNIFORM10,
                         simulations, seed)
        details[key] = {"名称": ind.NAME[key], "首位检验": t_first, "末位检验": t_last}
        tests.extend([t_first, t_last])

    blocked = [t["reason"] for t in tests if not t.get("available")]
    if blocked:
        return fail("可用数字不足：" + blocked[0], 明细=details, 忽略的零值个数=ignored)

    rejected = holm_reject([t["p"] for t in tests], alpha)
    q = round(1 - rejected / float(len(tests)), 4)
    out = {
        "Q": q,
        "available": True,
        "命中数": rejected,
        "检验数": len(tests),
        "alpha": alpha,
        "校正": "Holm",
        "模拟次数": simulations,
        "忽略的零值个数": ignored,
        "解释边界": ("Q 只表示数字分布未发现异常；不等于数据真实，也不是舞弊概率；"
                     "且它描述的是样本池而非受评对象"),
        "明细": details,
    }
    if q >= 0.9:
        # 实测：60 个干净样本池里 55 个 Q 恰为 1.0，实际取值区间 [0.93, 1.0]。
        # 融合层的 Q<0.3 / Q≥0.5 是在旧的 χ² 尺度上标定的，在新尺度下几乎必然放行。
        # 这是**口径问题**，不在算法层擅自改阈值，只如实提示。
        out["阈值适用性提示"] = (
            "本次 30 项检验仅 %d 项被拒绝，Q 落在 [0.9, 1]；"
            "融合层的 Q<0.3 / Q≥0.5 两个阈值是在旧 χ² 尺度上标定的，"
            "新尺度下判别力被削弱，需业务重新校准" % rejected
        )
    LOGGER.info("quality_gate 完成：Q=%s 拒绝=%d/%d", q, rejected, len(tests))
    return out
