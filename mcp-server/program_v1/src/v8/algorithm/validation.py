# -*- coding: utf-8 -*-
"""输入校验与规范化。

契约（全包统一，不得破坏）
--------------------------
* **参数错误**（调用方把 alpha 传成 1.5、树数传成字符串）→ 抛 ``ValidationError``。
  这是程序错误，不该被静默吞掉。
* **数据问题**（缺指标、NaN、样本不足、类别缺失）→ **不抛异常**，返回
  ``available=False`` + ``reason``。数据质量问题是业务事实，不是崩溃理由。

两条红线
--------
1. 私有化：日志与错误信息**只写类型和键名，不写数值**（财务比率属敏感数据）。
2. 不静默：任何被丢弃的输入都要在返回值里有计数和原因，绝不「缺了就当 0」。
"""
from __future__ import annotations

import math

from .. import indicators as ind

# 幅度上限：超过此值标准化会溢出，后续 z-score / LP 数值不可信
MAX_ABS = 1e100
# 最少样本：DEA-DA 需要两类各若干例才能建约束；留出校准集后还要够训练
MIN_POOL = 8
MIN_PER_CLASS = 8
# 留出校准集后每类最少训练样本
MIN_TRAIN_PER_CLASS = 4
# 单个数字检验的最少可用数字数（工程下限，不是统计充分性证明）
MIN_DIGITS = 8

# 参数取值域（集中定义，避免各处硬编码后漂移）
N_PER_GROUP_RANGE = (8, 200)
N_TREES_RANGE = (1, 500)
SIMULATIONS_RANGE = (99, 20000)


class ValidationError(ValueError):
    """参数非法。属于调用方缺陷，必须修调用方，不应在算法内部吞掉。"""

    def __init__(self, message: str, *, field: str = "", code: str = "invalid_argument"):
        super().__init__(message)
        self.field = field
        self.code = code


def is_number(value) -> bool:
    """真实数值判定：排除 bool、None、字符串、NaN、Inf。

    ``isinstance(True, int)`` 在 Python 里为真，业务上布尔不是数值，
    必须显式排除，否则 True 会被当成 1 参与计算。
    """
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value)))


def int_param(value, name: str, lo: int, hi: int) -> int:
    """校验整数参数（含边界），返回规范化后的 int。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("%s 必须是整数，收到 %s" % (name, type(value).__name__),
                              field=name)
    if not lo <= value <= hi:
        raise ValidationError("%s 必须落在 [%d, %d]，收到 %d" % (name, lo, hi, value),
                              field=name)
    return value


def float_param(value, name: str, lo: float, hi: float, *,
                lo_open: bool = False, hi_open: bool = False) -> float:
    """校验浮点参数（默认闭区间，可用 lo_open / hi_open 打开）。"""
    if not is_number(value):
        raise ValidationError("%s 必须是有限数值，收到 %s" % (name, type(value).__name__),
                              field=name)
    v = float(value)
    bad_lo = (v <= lo) if lo_open else (v < lo)
    bad_hi = (v >= hi) if hi_open else (v > hi)
    if bad_lo or bad_hi:
        raise ValidationError("%s 越界：%r 不在允许区间" % (name, v), field=name)
    return v


# ---------------------------------------------------------------- 值集合


def value_problems(values, *, keys=None, require_positive_x10: bool = True) -> list:
    """返回一组 15 个财务比率里的问题清单（空列表表示通过）。

    逐项检查而不是用 any() 短路，是为了一次把**所有**问题报给调用方，
    避免修一个报一个的往返。
    """
    keys = keys if keys is not None else ind.KEYS
    problems = []
    if not isinstance(values, dict):
        return ["取值必须是 dict，收到 %s" % type(values).__name__]
    missing = [k for k in keys if k not in values]
    if missing:
        problems.append("缺少指标：%s" % ",".join(missing))
    for k in keys:
        if k not in values:
            continue
        v = values[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            problems.append("指标 %s 非数值（%s）" % (k, type(v).__name__))
        elif not math.isfinite(float(v)):
            problems.append("指标 %s 非有限值（NaN/Inf）" % k)
        elif abs(float(v)) > MAX_ABS:
            problems.append("指标 %s 幅度超过 %g，标准化会溢出" % (k, MAX_ABS))
    if require_positive_x10 and is_number(values.get("X10")) and values["X10"] <= 0:
        # AR_DAYS = 365 / X10，X10 非正时旧实现返回 999 天哨兵值继续参与判别。
        # 那是「用假数据算真结论」，这里改为显式不可用。
        problems.append("X10 应收账款周转率须为正（AR_DAYS 依赖它）")
    return problems


def target_problems(target) -> list:
    """受评对象取值校验。"""
    if not isinstance(target, dict):
        return ["受评对象必须是 dict"]
    return value_problems(target)


def pool_problems(pool, *, min_per_class: int = MIN_PER_CLASS) -> list:
    """训练样本池校验：结构、标签、取值、类别规模。"""
    if not isinstance(pool, (list, tuple)):
        return ["样本池必须是 list"]
    if len(pool) < MIN_POOL:
        return ["训练样本不足 %d 例（当前 %d）" % (MIN_POOL, len(pool))]
    counts = {0: 0, 1: 0}
    for i, s in enumerate(pool):
        if not isinstance(s, dict):
            return ["第 %d 个样本不是 dict" % i]
        label = s.get("label")
        if isinstance(label, bool) or not isinstance(label, int) or label not in (0, 1):
            return ["第 %d 个样本标签非法（须为 0/1）" % i]
        counts[label] += 1
        problems = value_problems(s.get("values"))
        if problems:
            return ["第 %d 个样本：%s" % (i, problems[0])]
    if counts[0] < min_per_class or counts[1] < min_per_class:
        return ["每个类别至少 %d 例（当前 %d / %d）"
                % (min_per_class, counts[0], counts[1])]
    return []


def pool_signature(pool, *, keys=None, max_abs: float = MAX_ABS) -> str:
    """样本池指纹：用于模型缓存键与日志记录（不含企业标识）。

    用 repr 序列化是为了让不同来源的同一批数据得到同一指纹；
    不含 code/名称，避免把企业标识混进缓存键。
    """
    import hashlib

    keys = keys if keys is not None else ind.KEYS
    h = hashlib.sha256()
    h.update(str(len(pool)).encode("ascii"))
    for s in pool:
        h.update(str(int(s["label"])).encode("ascii"))
        for k in keys:
            v = s["values"][k]
            h.update(repr(round(float(v), 12)).encode("ascii"))
    return h.hexdigest()[:16]


def target_signature(target, *, keys=None) -> str:
    """受评对象指纹：用于稳定性结果缓存键。"""
    import hashlib

    keys = keys if keys is not None else ind.KEYS
    h = hashlib.sha256()
    for k in keys:
        h.update(repr(round(float(target[k]), 12)).encode("ascii"))
    return h.hexdigest()[:16]
