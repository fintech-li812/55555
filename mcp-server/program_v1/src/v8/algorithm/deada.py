# -*- coding: utf-8 -*-
"""第二层：改进 DEA-DA 判别，输出判别得分 D。

模型
----
两阶段 DEA-DA：求一组投入/产出权重 (v, u)，使两组样本被临界值 d 分开，
并最小化总误分类松弛 Σ(s⁺ + s⁻)，归一化约束 Σv + Σu = 1。
受评对象得分 = Σv·投入 + Σu·产出，``D = 得分 − 临界值``，D < 0 判为财务风险。

关键的工程约束
--------------
* **求解路径不得改动**：D 的三个锚点（healthy 0.868543 / edge 0.187676 /
  risk −0.471113）依赖 simplex.py:simplex_max 的确切 pivot 路径。
  换求解器会让锚点漂移，因此交叉验证用的是"另一条路径算出同一目标值"。
* **权重不可识别**：归一化下常有多重最优解，不同最优顶点给出不同权重。
  因此本模块把权重的可识别性如实标注为"未保证"，
  不允许上层把 AR_DAYS 权重 0.41 讲成"应收周转最重要"。
* **训练与打分分离**：LP 只依赖样本池，因此一次 fit 后可对任意多个
  受评对象反复打分——这是缓存能生效的前提。

前处理
------
AR_DAYS = −365 / X10（取负，使投入项统一为"越大越好"）；X13 资产负债率取负。
X10 ≤ 0 时旧实现返回 999 天哨兵值继续参与判别，那是**用假数据算真结论**，
本模块改为直接判不可用。
"""
from __future__ import annotations

import logging
import math
import random

from .. import indicators as ind
from . import simplex, validation
from .validation import is_number

LOGGER = logging.getLogger("v8.algorithm.deada")

INPUTS = list(ind.DEADA_INPUTS)      # ["X9", "X7", "AR_DAYS", "X13"]
OUTPUTS = list(ind.DEADA_OUTPUTS)    # ["X1", "X2", "X3", "X6"]
ALL_KEYS = INPUTS + OUTPUTS

BOOTSTRAP_SEED = 20261029
DEFAULT_REPEATS = 30
REPEATS_RANGE = (2, 200)


class DeadaModel:
    """DEA-DA 训练产物：与受评对象无关，可缓存复用。"""

    __slots__ = ("weights", "threshold", "norm", "diagnostics", "pool_size",
                 "fingerprint")

    def __init__(self, weights, threshold, norm, diagnostics, pool_size, fingerprint):
        self.weights = weights            # 未取整的权重向量（打分用）
        self.threshold = threshold        # 临界值 d
        self.norm = norm                  # {key: (mu, sd)}
        self.diagnostics = diagnostics
        self.pool_size = pool_size
        self.fingerprint = fingerprint

    @property
    def reported_weights(self) -> dict:
        """对外展示的权重（6 位小数）。仅用于审计展示，不参与打分。"""
        out = {}
        for i, k in enumerate(ALL_KEYS):
            out[k] = round(self.weights[i], 6)
        return out


def _ar_days(values: dict):
    """应收账款周转天数 = 365 / X10（用于投入项，取负号在外层加）。

    与基线不同：X10 非正时返回 None，而不是 999 天哨兵值。
    """
    r = values.get("X10")
    if not is_number(r) or r <= 0:
        return None
    return 365.0 / float(r)


def _prepare(values: dict):
    """前处理：派生 AR_DAYS 并对逆指标取负。不成立时返回 None。"""
    prepared = dict(values)
    ar = _ar_days(values)
    if ar is None:
        return None
    prepared["AR_DAYS"] = -ar
    if not is_number(values.get("X13")):
        return None
    prepared["X13"] = -float(values["X13"])
    for k in INPUTS + OUTPUTS:
        if k in ("AR_DAYS", "X13"):
            continue
        if not is_number(values.get(k)):
            return None
        prepared[k] = float(values[k])
    return prepared


def _zscore(rows, keys):
    """按列 z-score 标准化（总体标准差），消除比率与天数的量纲差异。"""
    stats = {}
    for k in keys:
        vals = [r[k] for r in rows]
        mu = sum(vals) / float(len(vals))
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / float(len(vals))) or 1.0
        stats[k] = (mu, sd)
    return stats


def fit(pool, *, verify: bool = True, max_iter: int = 4000):
    """训练 DEA-DA。返回 (DeadaModel | None, diagnostics)。

    失败时**不返回等权兜底值**：等权得分看起来正常，实则不可信，
    历史上正是这类"假 0"让融合层把无法判断当成无风险。
    """
    diag = {"solved": False, "optimality_verified": False}
    problems = validation.pool_problems(pool)
    if problems:
        diag["reason"] = problems[0]
        LOGGER.info("deada.fit 输入不可用：%s", problems[0])
        return None, diag

    rows, labels = [], []
    for s in pool:
        prepared = _prepare(s["values"])
        if prepared is None:
            diag["reason"] = "样本含非正应收周转率或非法指标"
            return None, diag
        rows.append(prepared)
        labels.append(int(s["label"]))

    stats = _zscore(rows, ALL_KEYS)
    Z = [{k: (r[k] - stats[k][0]) / stats[k][1] for k in ALL_KEYS} for r in rows]

    g1 = [z for z, lb in zip(Z, labels) if lb == 1]   # 风险组：期望低分
    g2 = [z for z, lb in zip(Z, labels) if lb == 0]   # 健康组：期望高分

    ni, no = len(INPUTS), len(OUTPUTS)
    nv = ni + no
    nvar = nv + 2 + len(g1) + len(g2)
    c = [0.0] * nvar
    for k in range(nv + 2, nvar):
        c[k] = -1.0                       # maximize −Σ(s⁺+s⁻) 即最小化误分类总量

    A, b = [], []
    for k1, row_z in enumerate(g1):       # 投入·v + 产出·u + s⁺ − d = 0
        row = [0.0] * nvar
        for i, k in enumerate(INPUTS):
            row[i] = row_z[k]
        for j, k in enumerate(OUTPUTS):
            row[ni + j] = row_z[k]
        row[nv] = -1.0
        row[nv + 1] = 1.0
        row[nv + 2 + k1] = 1.0
        A.append(row)
        b.append(0.0)
    for k2, row_z in enumerate(g2):       # 投入·v + 产出·u − s⁻ − d = 0
        row = [0.0] * nvar
        for i, k in enumerate(INPUTS):
            row[i] = row_z[k]
        for j, k in enumerate(OUTPUTS):
            row[ni + j] = row_z[k]
        row[nv] = -1.0
        row[nv + 1] = 1.0
        row[nv + 2 + len(g1) + k2] = -1.0
        A.append(row)
        b.append(0.0)
    row = [0.0] * nvar                    # Σv + Σu = 1
    for i in range(nv):
        row[i] = 1.0
    A.append(row)
    b.append(1.0)

    x, obj, ok, sdiag = simplex.solve_verified(c, A, b, max_iter=max_iter, verify=verify)
    diag.update(sdiag)
    if not ok or x is None:
        diag["reason"] = sdiag.get("reason", "线性规划未求得最优解")
        LOGGER.warning("deada.fit 求解失败：%s", diag["reason"])
        return None, diag

    model = DeadaModel(
        weights=list(x[:nv]),
        threshold=x[nv] - x[nv + 1],
        norm=stats,
        diagnostics=diag,
        pool_size=len(pool),
        fingerprint=validation.pool_signature(pool),
    )
    LOGGER.info("deada.fit 完成：池=%d 最优性确认=%s 目标差=%s",
                len(pool), diag.get("optimality_verified"),
                diag.get("objective_gap"))
    return model, diag


def score(model: DeadaModel, target):
    """对单个受评对象打分。返回 (D, 明细) 或 (None, 原因)。"""
    if model is None:
        return None, "模型不可用"
    problems = validation.target_problems(target)
    if problems:
        return None, problems[0]
    prepared = _prepare(target)
    if prepared is None:
        return None, "受评对象含非正应收周转率或非法指标"
    tz = {}
    for k in ALL_KEYS:
        mu, sd = model.norm[k]
        tz[k] = (prepared[k] - mu) / sd
    total = 0.0
    for i, k in enumerate(ALL_KEYS):
        total += model.weights[i] * tz[k]
    d = round(total - model.threshold, 6)
    detail = {
        "D": d,
        "判别得分": round(total, 6),
        "临界值": round(model.threshold, 6),
        "判为风险": d < 0,
        "权重": model.reported_weights,
        "投入指标": INPUTS,
        "产出指标": OUTPUTS,
        "样本": {"池规模": model.pool_size},
        "求解": "两阶段 Bland 单纯形（与基线逐位一致）",
        "求解器": model.diagnostics.get("solver", "未知"),
        "最优性确认": model.diagnostics.get("optimality_verified", False),
        "权重可识别性": "未保证：归一化约束下存在多重最优解，本权重只是其中一个最优顶点",
        "口径": "D = (Σv·投入 + Σu·产出) − 临界值；D < 0 判为财务风险",
        "来源": "马秀颖等，2026《数理统计与管理》改进 DEA-DA",
    }
    return d, detail


def fit_score(pool, target, *, verify: bool = True):
    """一次性训练并打分（不复用模型时的便捷入口）。"""
    model, diag = fit(pool, verify=verify)
    if model is None:
        return None, {"available": False, "reason": diag.get("reason", "训练失败"), "D": None}
    d, detail = score(model, target)
    if d is None:
        return None, {"available": False, "reason": detail, "D": None}
    return d, dict(detail, available=True)


# ---------------------------------------------------------------- 稳定性（探索性）


def stability(pool, target, *, repeats: int = DEFAULT_REPEATS, seed: int = BOOTSTRAP_SEED,
              verify: bool = False):
    """分层 bootstrap 看判别得分与权重稳不稳。

    默认 **verify=False**：30 次 LP 求解再各做一次对偶交叉验证太贵，
    而这是探索性指标，不参与决策，不需要证书。
    区间是经验分位区间，**不保证 95% 覆盖率**。
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int):
        return {"available": False, "reason": "repeats 必须是整数"}
    if not REPEATS_RANGE[0] <= repeats <= REPEATS_RANGE[1]:
        return {"available": False, "reason": "repeats 须落在 [%d, %d]" % REPEATS_RANGE}
    groups = [[s for s in pool if s.get("label") == lb] for lb in (0, 1)]
    if min(len(g) for g in groups) < 2:
        return {"available": False, "reason": "分层重采样需要每类至少 2 例"}

    rng = random.Random(seed)
    draws, weight_rows, failures = [], [], 0
    for _ in range(repeats):
        sample = [rng.choice(g) for g in groups for _ in g]
        model, _ = fit(sample, verify=verify)
        if model is None:
            failures += 1
            continue
        d, _ = score(model, target)
        if d is None:
            failures += 1
            continue
        draws.append(d)
        weight_rows.append(model.reported_weights)

    def interval(xs):
        if not xs:
            return None
        s = sorted(xs)
        return [s[int((len(s) - 1) * 0.025)], s[int((len(s) - 1) * 0.975)]]

    keys = sorted(weight_rows[0]) if weight_rows else []
    return {
        "available": bool(draws),
        "D_区间": interval(draws),
        "D_中位数": sorted(draws)[len(draws) // 2] if draws else None,
        "权重区间": {k: interval([w[k] for w in weight_rows]) for k in keys},
        "有效次数": len(draws),
        "失败次数": failures,
        "重复次数": repeats,
        "解释": ("探索性经验分位区间，不保证覆盖率；"
                 "区间跨 0 时不应把 D 的单点符号当结论——但融合表目前只消费符号，"
                 "这一局限已在上层如实标注"),
    }
