# -*- coding: utf-8 -*-
"""第三层：随机子空间 CART 森林，输出舞弊概率原始分。

与基线（v8/layers.py:benford_rf）的两处实质差异
------------------------------------------------
1. **特征不再消费第一层的"风险数字"**。
   基线把「首位数字是否等于该指标的风险数字」当特征，
   导致特征定义随样本池变化——换一批训练数据，同一家企业的特征就变了。
   本版改用**固定 Benford 理论分布**算首位数字自信息 −log f_B(d)，
   特征定义与样本池解耦。
2. **每棵树只在随机抽出的子空间里分裂**（随机子空间森林），
   而不是在全部 30 维上分裂，以此降低树间相关性。

刻意保留的
----------
* 树的构造、gini 计算、阈值取相邻值中点、max_depth=4 与基线**逐位一致**，
  这是 P 锚点可复现的前提；
* 自然类别频率（不做类别平衡）——训练池 1:1 是合成数据的性质，
  真实业务里强行平衡会破坏先验。

刻意不做的
----------
* 不分装成"模型文件"对外发布：树对象只在进程内缓存复用，
  对外只暴露结构化打分结果。
"""
from __future__ import annotations

import logging
import math
import random

from .. import indicators as ind
from .quality import BENFORD, first_digit
from .validation import MAX_ABS, is_number, value_problems

LOGGER = logging.getLogger("v8.algorithm.forest")

MAX_DEPTH = 4
# 每类留作校准的比例（至少 3 例）
CALIBRATION_FRACTION = 4
MIN_CALIBRATION_PER_CLASS = 3


# ---------------------------------------------------------------- 决策树


class Tree:
    """单棵 CART（基尼分裂，深度上限 4）。结构与基线实现逐位一致。"""

    __slots__ = ("max_depth", "root")

    def __init__(self, max_depth: int = MAX_DEPTH):
        self.max_depth = max_depth
        self.root = None

    @staticmethod
    def _gini(labels) -> float:
        n = len(labels)
        if n == 0:
            return 0.0
        p = sum(labels) / float(n)
        return 2 * p * (1 - p)

    def _build(self, X, y, depth):
        if depth >= self.max_depth or len(set(y)) == 1 or len(y) < 4:
            return {"leaf": True, "p": (sum(y) / float(len(y))) if y else 0.0}
        best = None
        for f in range(len(X[0])):
            vals = sorted(set(r[f] for r in X))
            for k in range(len(vals) - 1):
                thr = (vals[k] + vals[k + 1]) / 2.0
                l = [y[i] for i in range(len(y)) if X[i][f] <= thr]
                r = [y[i] for i in range(len(y)) if X[i][f] > thr]
                if not l or not r:
                    continue
                g = (len(l) * self._gini(l) + len(r) * self._gini(r)) / float(len(y))
                if best is None or g < best[0]:
                    best = (g, f, thr)
        if best is None:
            return {"leaf": True, "p": (sum(y) / float(len(y))) if y else 0.0}
        _, f, thr = best
        lX = [X[i] for i in range(len(y)) if X[i][f] <= thr]
        rX = [X[i] for i in range(len(y)) if X[i][f] > thr]
        ly = [y[i] for i in range(len(y)) if X[i][f] <= thr]
        ry = [y[i] for i in range(len(y)) if X[i][f] > thr]
        return {"leaf": False, "f": f, "thr": thr,
                "L": self._build(lX, ly, depth + 1),
                "R": self._build(rX, ry, depth + 1)}

    def fit(self, X, y):
        self.root = self._build(X, y, 0)
        return self

    def predict(self, x) -> float:
        node = self.root
        while node and not node["leaf"]:
            node = node["L"] if x[node["f"]] <= node["thr"] else node["R"]
        return node["p"] if node else 0.0


# ---------------------------------------------------------------- 特征


def features(values: dict):
    """30 维特征 = 15 个原始比率 + 15 个首位数字自信息。

    自信息用固定 Benford 理论分布，不随样本池变化。
    """
    raw = [float(values[k]) for k in ind.KEYS]
    surprise = []
    for k in ind.KEYS:
        dig = first_digit(values[k])
        surprise.append(-math.log(BENFORD[dig - 1]) if dig else 0.0)
    return raw + surprise


# ---------------------------------------------------------------- 森林


class ForestModel:
    """森林训练产物：与受评对象无关，可缓存复用。"""

    __slots__ = ("trees", "train_size", "oob_pairs", "n_features", "subspace")

    def __init__(self, trees, train_size, oob_pairs, n_features, subspace):
        self.trees = trees                  # ((cols, Tree), ...)
        self.train_size = train_size
        self.oob_pairs = oob_pairs          # [(预测, 真实标签), ...]
        self.n_features = n_features
        self.subspace = subspace


def _split_pool(pool, seed: int):
    """按类别分层留出校准集。返回 (训练集, 校准集)。"""
    groups = [[s for s in pool if s["label"] == lb] for lb in (0, 1)]
    rng = random.Random(seed)
    train, calibration = [], []
    for g in groups:
        shuffled = list(g)
        rng.shuffle(shuffled)
        take = max(MIN_CALIBRATION_PER_CLASS, len(shuffled) // CALIBRATION_FRACTION)
        calibration.extend(shuffled[:take])
        train.extend(shuffled[take:])
    return train, calibration


def fit(pool, *, n_trees: int = 60, seed: int = 20261027):
    """训练随机子空间森林。返回 (ForestModel | None, 校准集, 原因)。"""
    if not isinstance(pool, (list, tuple)) or len(pool) < 8:
        return None, None, "样本池规模不足"
    for s in pool:
        if not isinstance(s, dict) or not isinstance(s.get("values"), dict):
            return None, None, "样本结构非法"
        if isinstance(s.get("label"), bool) or s.get("label") not in (0, 1):
            return None, None, "样本标签非法（须为 0/1）"
        if not all(is_number(s["values"].get(k)) and abs(float(s["values"][k])) <= MAX_ABS
                   for k in ind.KEYS):
            return None, None, "训练样本含非法或非有限指标值"

    train, calibration = _split_pool(pool, seed)
    counts = {lb: sum(1 for s in train if s["label"] == lb) for lb in (0, 1)}
    if min(counts.values()) < 4:
        return None, None, "留出校准集后每类训练样本不足 4 例"

    X = [features(s["values"]) for s in train]
    y = [int(s["label"]) for s in train]
    n_features = len(X[0])
    subspace = max(1, int(math.sqrt(n_features)))

    rng = random.Random(seed)
    models = []
    oob = [[] for _ in train]
    for _ in range(n_trees):
        idx = [rng.randrange(len(train)) for _ in train]
        cols = sorted(rng.sample(range(n_features), subspace))
        tree = Tree(MAX_DEPTH).fit([[X[i][j] for j in cols] for i in idx],
                                   [y[i] for i in idx])
        models.append((tuple(cols), tree))
        used = set(idx)
        for i in range(len(train)):
            if i not in used:
                oob[i].append(tree.predict([X[i][j] for j in cols]))

    oob_pairs = []
    for i, votes in enumerate(oob):
        if votes:
            oob_pairs.append((sum(votes) / float(len(votes)), y[i]))

    LOGGER.info("forest.fit 完成：树数=%d 训练=%d 校准=%d 子空间=%d/%d",
                len(models), len(train), len(calibration), subspace, n_features)
    return ForestModel(tuple(models), len(train), oob_pairs, n_features, subspace), \
        calibration, None


def predict_raw(model: ForestModel, values: dict) -> float:
    """森林原始分（未校准），即各树预测概率的均值。"""
    if model is None or not model.trees:
        return 0.0
    x = features(values)
    total = 0.0
    for cols, tree in model.trees:
        total += tree.predict([x[j] for j in cols])
    return total / float(len(model.trees))


def train(pool, *, n_trees: int = 60, seed: int = 20261027):
    """训练森林并在留出集上做概率校准。

    森林与校准**都只依赖样本池**，因此可以作为缓存单元；
    对单个受评对象打分只是 60 次树遍历，与池规模无关。
    返回 (ForestModel | None, 校准结果 | None, 原因)。
    """
    from . import calibration as cal

    model, calibration_pool, reason = fit(pool, n_trees=n_trees, seed=seed)
    if model is None:
        return None, None, reason
    cal_scores = [predict_raw(model, s["values"]) for s in calibration_pool]
    calib = cal.fit(cal_scores, [int(s["label"]) for s in calibration_pool])
    return model, calib, None


def fit_score(pool, target, *, n_trees: int = 60, seed: int = 20261027):
    """一次性训练、校准并对单个对象打分。返回完整第三层结果。"""
    from . import calibration as cal

    def fail(reason: str) -> dict:
        LOGGER.info("forest 不可用：%s", reason)
        return {"P": None, "available": False, "reason": reason, "树数": 0}

    if not isinstance(target, dict):
        return fail("受评对象必须是 dict")
    problems = value_problems(target)
    if problems:
        return fail("受评对象：" + problems[0])

    model, calib, reason = train(pool, n_trees=n_trees, seed=seed)
    if model is None:
        return fail(reason)
    return score(model, calib, target, n_trees=n_trees)


def score(model, calib, target, *, n_trees: int = 60):
    """对单个受评对象打分（模型与校准已就绪）。"""
    from . import calibration as cal

    raw = predict_raw(model, target)
    result = {
        "P_raw": round(raw, 6),
        "树数": len(model.trees),
        "训练样本": model.train_size,
        "特征维度": model.n_features,
        "特征构成": "15 个财务比率 + 15 个首位数字自信息（固定 Benford 理论分布）",
        "参数": {"n_trees": n_trees, "max_depth": MAX_DEPTH,
                 "子空间": "%d/%d" % (model.subspace, model.n_features),
                 "bootstrap": "普通（有放回）", "class_weight": "无，保留自然频率"},
        "OOB_覆盖": (round(len(model.oob_pairs) / float(model.train_size), 4)
                     if model.train_size else None),
    }
    brier = cal.brier(model.oob_pairs) if model.oob_pairs else None
    ece = cal.ece(model.oob_pairs) if model.oob_pairs else None
    result["OOB_Brier"] = round(brier, 6) if brier is not None else None
    result["OOB_ECE"] = round(ece, 6) if ece is not None else None

    if not calib.get("available"):
        result.update({"P": None, "available": False,
                       "reason": "概率校准不可用：" + calib.get("reason", "未知原因"),
                       "校准": calib})
        return result

    p = cal.predict(raw, calib["a"], calib["b"])
    result.update({
        "P": round(p, 4),
        "available": True,
        "校准": {
            "方法": "独立分层留出 + 正则 sigmoid（a≥0 保单调）",
            "样本数": calib["n"],
            "a": round(calib["a"], 6),
            "b": round(calib["b"], 6),
            "收敛": calib["converged"],
            "梯度残差": round(calib["projected_gradient"], 8),
            "样本充足": calib["sufficient_n"],
            "充足门槛": calib["min_n_policy"],
        },
    })
    return result
