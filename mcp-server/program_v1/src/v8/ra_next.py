# -*- coding: utf-8 -*-
"""RA_NEXT 算法（ra-next-0.1）生产适配层。

来源：`algo/RA_NEXT/reference.py`（Collex 交付的离线研究候选）。
本文件是**工程侧适配**，不是重新设计算法：数值口径与候选逐位一致，
仅补齐生产所需的三件事——

1. **可验证性**：每个不可用场景都返回 ``available=False`` + ``reason``，
   不返回"看起来正常的 0"；调用方必须据此走降级，而不是当无风险。
2. **输入防御**：非数值 / NaN / Inf / 缺键 / 类别不足，一律判不可用，
   不让 KeyError、TypeError 冒到 HTTP 层。
3. **确定性**：所有随机过程固定 seed，不使用时间、哈希顺序、环境相关的量。

相对基线（``v8/layers.py``）的算法改动点
------------------------------------------
Q：χ² 临界值判定 → 多项分布 Monte Carlo 精确 p 值（1999 次），
   30 项检验用 Holm 逐步法控制族错误率，``Q = 1 − 拒绝数 / 30``。
D：沿用基线 Bland 单纯形与点估计（**结果与基线逐位相同**），
   仅把求解失败时的 D 置为 None（基线的降级等权值仍保留在返回值里供审计）。
P：训练池风险数字 → 固定首位数字自信息 ``−log f_B(d)``（解耦 A5）；
   全特征森林 → 每树随机抽 5/30 维（随机子空间）；
   新增 OOB 评估与**独立分层校准集**（每类 1/4，最少 3 例）上的
   正则 sigmoid 校准；``P_raw`` 与 ``P`` 同时返回。

已知边界（不得在工程侧伪装已解决）
----------------------------------
* Q 的尺度变了（同一数据 0.5667 → 0.9333），融合层 0.3/0.5 两个阈值是在
  旧尺度上定的，**未重新校准**。适配层用 ``阈值适用性`` 字段如实提示，
  但不擅自改阈值——改阈值属口径裁决，须业务确认。
* Benford 律对**有界比率**的适用性本身未被证明，Q 只能解释为
  "数字分布未发现异常"，不能解释为"数据真实"。
* 校准集仅 10 例（合成），校准参数不可外推；无 Linux 执行证据。
"""
from __future__ import annotations

import math
import random
from bisect import bisect_left
from functools import lru_cache

from . import indicators as ind
from . import layers as base

VERSION = "ra-next-0.1"
MC_SEED = 20261029
MC_SIMULATIONS = 1999
DEFAULT_ALPHA = 0.05
Q_MIN_DIGITS = 8            # 单个检验的最少可用数字数（工程下限，非有效性证明）

# 每指标 2 项检验（首位 9 格 / 末位 10 格）
_TESTS_PER_INDICATOR = 2
_N_TESTS = _TESTS_PER_INDICATOR * len(ind.KEYS)


def _finite(value) -> bool:
    """生产环境对布尔、字符串、None、NaN、Inf 一律不认。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _unavailable(reason: str, **extra) -> dict:
    out = {"available": False, "reason": reason, "版本": VERSION}
    out.update(extra)
    return out


# ---------------------------------------------------------------- 第一层：Q

@lru_cache(maxsize=256)
def _null_distribution(n: int, probabilities: tuple, simulations: int, seed: int) -> tuple:
    """多项分布零假设下的检验统计量经验分布（升序）。

    说明：同一 ``(n, probabilities, simulations, seed)`` 只算一次。
    15 个指标共用同一组理论频率（Benford 9 格 / 均匀 10 格），
    因此实际只算 2 次，这是 1999 次模拟仍能在百毫秒级完成的原因。
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


def mc_test(counts: list, probabilities: list, simulations: int = MC_SIMULATIONS,
            seed: int = MC_SEED) -> dict:
    """多项分布拟合优度的 Monte Carlo 检验，返回 (p 值 + MC 误差区间)。"""
    n = int(sum(counts))
    if len(counts) != len(probabilities):
        return _unavailable("counts 与 probabilities 长度不一致")
    if n < Q_MIN_DIGITS:
        return _unavailable("可用数字不足 %d 个" % Q_MIN_DIGITS)
    if any(p <= 0 for p in probabilities):
        return _unavailable("理论频率必须为正")
    stat = sum((c - n * p) ** 2 / (n * p) for c, p in zip(counts, probabilities))
    null = _null_distribution(n, tuple(probabilities), simulations, seed)
    hits = len(null) - bisect_left(null, stat - 1e-12)
    p_value = (hits + 1) / (len(null) + 1)          # 加一修正，避免 p=0
    # Wilson 区间只量化「模拟次数」带来的误差，不是 Q 的置信区间。
    ph = hits / len(null)
    z = 1.959963984540054
    den = 1 + z * z / len(null)
    center = (ph + z * z / (2 * len(null))) / den
    width = z * math.sqrt(ph * (1 - ph) / len(null) + z * z / (4 * len(null) ** 2)) / den
    return {"available": True, "p": p_value, "统计量": stat, "n": n,
            "mc_区间": [max(0.0, center - width), min(1.0, center + width)],
            "模拟次数": simulations}


def quality_gate(pool: list, *, alpha: float = DEFAULT_ALPHA,
                 simulations: int = MC_SIMULATIONS, seed: int = MC_SEED) -> dict:
    """第一层数据质量门禁：30 项数字分布检验 + Holm 校正 → Q。

    ``Q = 1 − 拒绝数 / 30``，越大表示数字分布越接近理论分布。
    **无论可用与否，返回体都带 ``Q`` 键**（不可用时为 None），
    否则调用方 ``gate["Q"]`` 会在异常分支上 KeyError。
    """
    def _q_unavailable(reason: str, **extra) -> dict:
        return dict(_unavailable(reason, **extra), Q=None)

    if not 0 < alpha < 1:
        return _q_unavailable("alpha 必须落在 (0,1)，收到 %r" % (alpha,))
    if not isinstance(simulations, int) or simulations < 99:
        return _q_unavailable("模拟次数须为 ≥99 的整数")
    if not pool:
        return _q_unavailable("样本池为空")

    details, tests = {}, []
    for key in ind.KEYS:
        vals = [s.get("values", {}).get(key) for s in pool]
        if not all(_finite(v) for v in vals):
            return _q_unavailable("指标 %s 存在非数值或非有限值" % key, 明细={})
        first = [base.first_digit(v) for v in vals]
        last = [base.last_digit(v) for v in vals]
        t_first = mc_test([first.count(d) for d in range(1, 10)], list(base.BENFORD),
                          simulations, seed)
        t_last = mc_test([last.count(d) for d in range(10)], [0.1] * 10, simulations, seed)
        details[key] = {"名称": ind.NAME[key], "首位检验": t_first, "末位检验": t_last}
        tests.extend([t_first, t_last])

    unavailable = [t["reason"] for t in tests if not t.get("available")]
    if unavailable:
        return _q_unavailable("可用数字不足：" + unavailable[0], 明细=details)

    # Holm 逐步法：控制族错误率，不假设各检验独立。
    order = sorted(range(len(tests)), key=lambda i: tests[i]["p"])
    rejected = 0
    for rank, idx in enumerate(order):
        if tests[idx]["p"] <= alpha / (len(tests) - rank):
            rejected += 1
        else:
            break

    q = round(1 - rejected / float(len(tests)), 4)
    out = {
        "Q": q, "available": True, "版本": VERSION,
        "命中数": rejected, "检验数": len(tests), "alpha": alpha, "校正": "Holm",
        "口径": "Q = 1 − 拒绝数 / 30；拒绝=该指标数字分布与理论分布不符",
        "解释边界": "仅表示数字分布未发现异常，不等于数据真实，也不是舞弊概率",
        "适用性": "Benford 律对有界比率的适用性需另行论证",
        "明细": details,
    }
    if q >= 0.9:
        out["阈值适用性"] = (
            "本次 30 项检验仅 %d 项被拒绝，Q 落在 [0.9,1]；"
            "融合层的 Q<0.3 / Q≥0.5 两个阈值是在旧的 χ² 尺度上标定的，"
            "在新尺度下几乎必然放行，判别力被削弱，需重新校准"
            "（属口径裁决，未擅自修改）" % rejected
        )
    return out


# ---------------------------------------------------------------- 第二层：D

def deada(pool: list, target: dict) -> dict:
    """第二层：沿用基线 DEA-DA（Bland 单纯形），仅补输入防御与不可用语义。"""
    if not isinstance(pool, list) or len(pool) < 8:
        return _unavailable("训练样本不足 8 例", D=None, 降级=True)
    if {s.get("label") for s in pool} != {0, 1}:
        return _unavailable("训练样本须同时包含 0/1 两类", D=None, 降级=True)
    for s in pool:
        if not all(_finite(s.get("values", {}).get(k)) for k in ind.KEYS):
            return _unavailable("训练样本存在非数值或非有限值", D=None, 降级=True)
    if not isinstance(target, dict) or not all(_finite(target.get(k)) for k in ind.KEYS):
        return _unavailable("受评样本缺少指标或存在非法数值", D=None, 降级=True)

    result = base.deada(pool, target)
    if result.get("降级"):
        # 基线的等权降级值仍保留在 D_降级参考里，但对外 D 置 None，
        # 避免融合层把不可信的等权得分当成真实判别结果。
        return {"D": None, "降级": True, "available": False, "版本": VERSION,
                "reason": "线性规划未求得最优解，判别得分不可信",
                "D_降级参考": result.get("D"), "权重": result.get("权重")}
    result["available"] = True
    result["版本"] = VERSION
    return result


def deada_stability(pool: list, target: dict, repeats: int = 30,
                    seed: int = MC_SEED) -> dict:
    """分层 bootstrap：看判别得分与权重到底稳不稳。

    30 次只作**探索性稳定性**，经验分位区间不保证 95% 覆盖率。
    """
    if not isinstance(repeats, int) or not 2 <= repeats <= 200:
        return _unavailable("repeats 须为 [2,200] 的整数")
    groups = [[s for s in pool if s.get("label") == label] for label in (0, 1)]
    if min(len(g) for g in groups) < 2:
        return _unavailable("分层 bootstrap 需要每类至少 2 例")

    rng = random.Random(seed)
    draws, weight_rows, failed = [], [], 0
    for _ in range(repeats):
        sample = [rng.choice(g) for g in groups for _ in g]
        r = deada(sample, target)
        if not r.get("available"):
            failed += 1
            continue
        draws.append(r["D"])
        weight_rows.append(r["权重"])

    def interval(xs):
        if not xs:
            return None
        s = sorted(xs)
        return [s[int((len(s) - 1) * 0.025)], s[int((len(s) - 1) * 0.975)]]

    keys = sorted(weight_rows[0]) if weight_rows else []
    return {
        "available": bool(draws), "版本": VERSION,
        "D_区间": interval(draws), "D_中位数": sorted(draws)[len(draws) // 2] if draws else None,
        "权重区间": {k: interval([w[k] for w in weight_rows]) for k in keys},
        "有效次数": len(draws), "失败次数": failed, "重复次数": repeats,
        "解释": "探索性经验分位区间，不保证覆盖率；区间跨 0 时不应把 D 的单点符号当结论",
    }


# ---------------------------------------------------------------- 第三层：P

def _features(values: dict) -> list:
    """30 维特征：15 个原始比率 + 15 个首位数字自信息。

    自信息用**固定 Benford 理论分布**计算，不依赖训练池算出的"风险数字"，
    这是相对基线最实质的一处解耦（基线会随池子变化而改变特征定义）。
    """
    raw = [values[k] for k in ind.KEYS]
    surprise = []
    for k in ind.KEYS:
        d = base.first_digit(values[k])
        surprise.append(-math.log(base.BENFORD[d - 1]) if d else 0.0)
    return raw + surprise


def _forest(pool: list, n_trees: int, seed: int):
    """随机子空间森林：每棵树抽 len(KEYS)**0.5 维，返回 (模型, OOB 预测)。"""
    rng = random.Random(seed)
    X = [_features(s["values"]) for s in pool]
    y = [s["label"] for s in pool]
    n_features = len(X[0])
    k = max(1, int(math.sqrt(n_features)))
    models, oob = [], [[] for _ in pool]
    for _ in range(n_trees):
        idx = [rng.randrange(len(pool)) for _ in pool]
        cols = sorted(rng.sample(range(n_features), k))
        tree = base._Tree(4).fit([[X[i][j] for j in cols] for i in idx], [y[i] for i in idx])
        models.append((cols, tree))
        used = set(idx)
        for i in range(len(pool)):
            if i not in used:
                oob[i].append(tree.predict([X[i][j] for j in cols]))
    return models, oob


def _predict(models: list, values: dict) -> float:
    x = _features(values)
    return sum(t.predict([x[j] for j in c]) for c, t in models) / float(len(models))


def _sigmoid(p, a: float, b: float) -> float:
    clipped = max(1e-4, min(1 - 1e-4, p))
    z = max(-35.0, min(35.0, a * math.log(clipped / (1 - clipped)) + b))
    return 1 / (1 + math.exp(-z))


def sigmoid_fit(scores: list, labels: list, steps: int = 1000, lr: float = 0.05,
                l2: float = 0.1) -> tuple:
    """正则 sigmoid 校准：概率裁剪 + 单调约束 a≥0。

    isotonic 在 10 例校准集上过拟合（实验记录见
    ``algo/RA_NEXT/experiment_isotonic_rejected.json``），故改用两参数 sigmoid。
    """
    a, b = 1.0, 0.0
    n = float(len(scores))
    for _ in range(steps):
        da, db = l2 * a, l2 * b          # L2 项梯度，先初始化再累加
        for p, y in zip(scores, labels):
            clipped = max(1e-4, min(1 - 1e-4, p))
            err = _sigmoid(p, a, b) - y
            da += err * math.log(clipped / (1 - clipped)) / n
            db += err / n
        a = max(0.0, a - lr * da)        # a≥0 保证校准后仍单调
        b -= lr * db
    return a, b


def benford_rf(pool: list, target: dict, gate: dict | None = None,
               n_trees: int = 60, seed: int = 20261027) -> dict:
    """第三层：随机子空间森林 + 独立校准集 → 舞弊概率 P。

    参数 ``gate`` 保留仅为兼容基线签名；本版特征不再消费第一层的风险数字。
    """
    groups = [[s for s in pool if s.get("label") == label] for label in (0, 1)]
    if n_trees < 1:
        return _unavailable("树数为 0，概率无意义", P=None, 树数=0)
    if min(len(g) for g in groups) < 8:
        return _unavailable("每类样本须 ≥8 例才能留出校准集", P=None, 树数=0)
    if not isinstance(target, dict) or not all(_finite(target.get(k)) for k in ind.KEYS):
        return _unavailable("受评样本缺少指标或存在非法数值", P=None, 树数=0)

    rng = random.Random(seed)
    train, calibration = [], []
    for g in groups:
        shuffled = list(g)
        rng.shuffle(shuffled)
        take = max(3, len(shuffled) // 4)
        calibration.extend(shuffled[:take])
        train.extend(shuffled[take:])
    if min(len([s for s in train if s["label"] == l]) for l in (0, 1)) < 4:
        return _unavailable("留出校准集后训练样本不足", P=None, 树数=0)

    models, oob = _forest(train, n_trees, seed)
    if not models:
        return _unavailable("未训练出任何树", P=None, 树数=0)

    cal_scores = [_predict(models, s["values"]) for s in calibration]
    params = sigmoid_fit(cal_scores, [s["label"] for s in calibration])
    raw = _predict(models, target)
    calibrated = _sigmoid(raw, *params)

    pairs = [(sum(v) / len(v), train[i]["label"]) for i, v in enumerate(oob) if v]
    brier = (sum((p - y) ** 2 for p, y in pairs) / len(pairs)) if pairs else None
    return {
        "P": round(calibrated, 4), "P_raw": round(raw, 4), "available": True,
        "树数": len(models), "版本": VERSION,
        "校准": {"方法": "独立分层留出 + 正则 sigmoid（a≥0 保单调）",
               "样本数": len(calibration), "a": round(params[0], 6), "b": round(params[1], 6),
               "局限": "校准集仅 %d 例且为合成数据，参数不可外推" % len(calibration)},
        "OOB_Brier": round(brier, 6) if brier is not None else None,
        "OOB_覆盖": round(len(pairs) / float(len(train)), 4) if train else None,
        "特征维度": len(_features(target)),
        "特征构成": "15 个财务比率 + 15 个首位数字自信息（固定 Benford 理论分布）",
        "参数": {"n_trees": n_trees, "max_depth": 4, "每树特征数": "sqrt(30)≈5",
               "bootstrap": "普通（有放回）", "class_weight": "无，保留自然频率"},
        "口径": "概率是「合成风险标签」的概率，不是真实舞弊概率；P≥0.7 仍按融合表处理",
    }
