# -*- coding: utf-8 -*-
"""概率校准：两参数 sigmoid（Platt scaling）。

为什么是两参数而不是保序回归
----------------------------
保序回归在 10 例校准集上会过拟合（失败证据保留在
``algo/RA_NEXT/experiment_isotonic_rejected.json``），
因此改用带 L2 正则的 sigmoid，并把斜率约束为 a≥0 以**保持单调**——
打分高的样本校准后仍不能低于打分低的样本。

对外暴露「是否收敛」，但不把"收敛"当作"校准可靠"
--------------------------------------------------
收敛只是优化问题解出来了；样本量、代表性与真实业务校准是**另一回事**。
三件事分开报，由上层 policy 组装证据等级。
"""
from __future__ import annotations

import logging
import math

from .validation import is_number

LOGGER = logging.getLogger("v8.algorithm.calibration")

# 裁剪上下限：避免 logit 溢出到 ±inf
CLIP = 1e-4
# logit 截断：exp(±35) 已在双精度可用范围内
LOGIT_MAX = 35.0

DEFAULT_STEPS = 1000
DEFAULT_LR = 0.05
DEFAULT_L2 = 0.1
# 投影梯度无穷范数的收敛门槛
CONVERGE_TOL = 1e-5
# 视为"校准证据充分"的最小样本数（策略门槛，非统计充分性证明）
MIN_CALIBRATION_N = 30


def _sigmoid(p: float, a: float, b: float) -> float:
    clipped = max(CLIP, min(1 - CLIP, p))
    z = max(-LOGIT_MAX, min(LOGIT_MAX, a * math.log(clipped / (1 - clipped)) + b))
    return 1.0 / (1.0 + math.exp(-z))


def sigmoid_fit(scores, labels, *, steps: int = DEFAULT_STEPS, lr: float = DEFAULT_LR,
                l2: float = DEFAULT_L2, early_stop_tol=None) -> tuple:
    """拟合 a, b。默认固定 1000 步，与历史版本逐位一致。

    ``early_stop_tol`` 默认关闭：一旦提前停止，(a, b) 会与固定步数版本不同，
    历史锚点就对不上。需要早停的调用方必须自己验证锚点漂移。
    """
    a, b = 1.0, 0.0
    n = float(len(scores))
    for step in range(steps):
        da, db = l2 * a, l2 * b
        for p, y in zip(scores, labels):
            clipped = max(CLIP, min(1 - CLIP, p))
            err = _sigmoid(p, a, b) - y
            da += err * math.log(clipped / (1 - clipped)) / n
            db += err / n
        a = max(0.0, a - lr * da)
        b -= lr * db
        if early_stop_tol is not None and step > 10:
            if max(abs(da), abs(db)) < early_stop_tol:
                break
    return a, b


def sigmoid_diagnostics(parameters, scores, labels, tol: float = CONVERGE_TOL) -> dict:
    """投影梯度收敛诊断。

    a 在边界（a=0）时梯度要投影到可行方向，否则会把"约束在起作用"
    误判成"还没收敛"。
    """
    a, b = parameters
    da, db = 0.1 * a, 0.1 * b
    n = float(len(scores))
    for p, y in zip(scores, labels):
        clipped = max(CLIP, min(1 - CLIP, p))
        err = _sigmoid(p, a, b) - y
        da += err * math.log(clipped / (1 - clipped)) / n
        db += err / n
    projected_a = da if a > 0 else min(0.0, da)
    residual = max(abs(projected_a), abs(db))
    return {
        "projected_gradient": residual,
        "converged": residual <= tol,
        "tolerance": tol,
        "reason": ("凸目标的投影梯度检查通过；样本量是否充分是另一个门槛"
                   if residual <= tol else "梯度残差 %g 超过容差 %g" % (residual, tol)),
    }


def fit(scores, labels, *, steps: int = DEFAULT_STEPS, lr: float = DEFAULT_LR,
        l2: float = DEFAULT_L2) -> dict:
    """完整校准结果：参数 + 收敛诊断 + 证据门槛。"""
    clean = [(float(p), int(y)) for p, y in zip(scores, labels)
             if is_number(p) and isinstance(y, int)]
    if len(clean) < 2:
        return {"available": False, "reason": "校准样本不足 2 例", "n": len(clean)}
    if len({y for _, y in clean}) < 2:
        return {"available": False, "reason": "校准样本只有单一类别，无法拟合",
                "n": len(clean)}
    xs = [p for p, _ in clean]
    ys = [y for _, y in clean]
    params = sigmoid_fit(xs, ys, steps=steps, lr=lr, l2=l2)
    diag = sigmoid_diagnostics(params, xs, ys)
    return {
        "available": True,
        "a": params[0],
        "b": params[1],
        "n": len(clean),
        "converged": diag["converged"],
        "projected_gradient": diag["projected_gradient"],
        "sufficient_n": len(clean) >= MIN_CALIBRATION_N,
        "min_n_policy": MIN_CALIBRATION_N,
        "reason": diag["reason"],
    }


def predict(p: float, a: float, b: float) -> float:
    if not is_number(p):
        return 0.0
    return _sigmoid(max(0.0, min(1.0, float(p))), a, b)


def brier(pairs) -> float | None:
    """Brier = mean((p−y)²)。pairs 为 (预测概率, 0/1 标签) 序列。"""
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / float(len(pairs))


def ece(pairs, bins: int = 10) -> float | None:
    """期望校准误差：等宽分箱后按样本数加权的 |平均预测 − 实际频率|。"""
    if not pairs or bins < 2:
        return None
    # 每箱累计 (预测概率之和, 真实标签之和, 样本数)
    buckets = {}
    for p, y in pairs:
        idx = min(int(p * bins), bins - 1)
        sp, sy, n = buckets.get(idx, (0.0, 0.0, 0))
        buckets[idx] = (sp + p, sy + y, n + 1)
    total_n = sum(n for _, _, n in buckets.values())
    if not total_n:
        return None
    acc = 0.0
    for sp, sy, n in buckets.values():
        # 箱内平均预测置信度 vs 箱内实际正例频率
        acc += n * abs(sp / n - sy / n)
    return acc / total_n
