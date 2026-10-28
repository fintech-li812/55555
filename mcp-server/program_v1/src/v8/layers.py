# -*- coding: utf-8 -*-
"""v8 三层风控模型（纯 Python 标准库实现，工程零外部依赖）。

第一层 数据质量门禁：Benford 律首位数字 χ² 检验 + Myer 指数末位数字检验 → 质量得分 Q
    （方法来源：杨贵军等，2022，《统计与决策》BM-BP 模型）
第二层 财务健康判别：改进 DEA-DA，两阶段线性规划求解投入/产出权重 → 判别得分 D
    （方法来源：马秀颖等，2026，《数理统计与管理》改进 DEA-DA）
    线性规划由本模块自带的两阶段单纯形法求解；若求解失败则如实降级并标注。
第三层 异常样本复核：Benford 因子 + CART 随机森林 → 舞弊概率 P
    （方法来源：孙玲莉等，2021，《数量经济技术经济研究》Benford-随机森林）
"""
from __future__ import annotations

import math
import random

from . import indicators as ind

# ---------------------------------------------------------------- 第一层


def first_digit(x: float):
    """取首位有效数字（1~9）。"""
    if x is None:
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


def last_digit(x: float):
    """取末位数字（0~9），按两位小数对齐。"""
    if x is None:
        return None
    s = "%.6f" % abs(float(x))
    s = s.rstrip("0")
    for ch in reversed(s):
        if ch.isdigit():
            return int(ch)
    return None


BENFORD = [math.log10(1.0 + 1.0 / d) for d in range(1, 10)]  # f_B(1..9)


def benford_chi2(values: list):
    """Benford 律 χ² 拟合优度检验。临界值 20.09（10% 显著性水平）。"""
    obs = [0] * 9
    n = 0
    for v in values:
        d = first_digit(v)
        if d:
            obs[d - 1] += 1
            n += 1
    if n == 0:
        return {"chi2": 0.0, "n": 0, "偏离": False, "obs": obs, "risk_digit": None}
    chi2 = 0.0
    max_dev, risk_digit = -1.0, None
    for i in range(9):
        e = obs[i] / float(n)
        f = BENFORD[i]
        chi2 += n * ((e - f) ** 2) / f
        dev = abs(e - f)
        if dev > max_dev:
            max_dev, risk_digit = dev, i + 1
    return {"chi2": chi2, "n": n, "偏离": chi2 > 20.09,
            "obs": obs, "risk_digit": risk_digit}


def myer_index(values: list):
    """末位数字检验：Myer 混合指数 + 末位均匀分布 χ² 检验。

    Myer 混合指数 M = 0.5 × Σ|P1j − P2j|（按值大小分两组比较末位分布），
    文献判据为 M > 20。但该指数对样本量敏感——小样本下即使数据干净，
    频率抽样波动也会让 M 系统性偏高。因此本工程：

      · Myer 指数数值照实计算并展示；
      · 是否判定"末位堆积"改用末位数字对均匀分布的 χ² 检验
        （df=9，5% 显著性临界值 16.92），该判据在小样本下稳健。

    这一取舍会在结果中如实标注，不伪装成文献原始判据。
    """
    xs = sorted([v for v in values if v is not None])
    if len(xs) < 4:
        return {"myer": 0.0, "末位卡方": 0.0, "堆积": False,
                "risk_digit": None, "n": len(xs)}
    half = len(xs) // 2
    g1, g2 = xs[:half], xs[half:]

    def pct(group):
        c = [0] * 10
        for v in group:
            d = last_digit(v)
            if d is not None:
                c[d] += 1
        t = float(sum(c)) or 1.0
        return [c[i] / t * 100.0 for i in range(10)]

    p1, p2 = pct(g1), pct(g2)
    m = 0.5 * sum(abs(p1[i] - p2[i]) for i in range(10))

    cnt = [0] * 10
    for v in xs:
        d = last_digit(v)
        if d is not None:
            cnt[d] += 1
    tot = float(sum(cnt)) or 1.0
    exp = tot / 10.0
    chi2_u = sum(((c - exp) ** 2) / exp for c in cnt) if exp > 0 else 0.0
    risk_digit = max(range(10), key=lambda i: cnt[i])
    return {"myer": m, "末位卡方": chi2_u, "堆积": chi2_u > 16.92,
            "risk_digit": risk_digit, "n": len(xs), "p1": p1, "p2": p2}


def quality_gate(pool: list):
    """第一层：对 15 个指标逐一做 Benford + Myer 检验，输出数据质量得分 Q。"""
    per = {}
    sum_b = sum_m = 0
    for key, name, nature in ind.INDICATORS:
        vals = [s["values"][key] for s in pool]
        bf = benford_chi2(vals)
        my = myer_index(vals)
        # 构造 0/1 质量因子：该指标是否命中"风险数字"
        b_hit = 1 if (bf["偏离"] and bf["risk_digit"]) else 0
        m_hit = 1 if (my["堆积"] and my["risk_digit"] is not None) else 0
        sum_b += b_hit
        sum_m += m_hit
        per[key] = {
            "名称": name, "性质": nature,
            "benford_chi2": round(bf["chi2"], 3), "benford_偏离": bf["偏离"],
            "benford_风险数字": bf["risk_digit"],
            "myer指数": round(my["myer"], 3), "末位卡方": round(my["末位卡方"], 3),
            "myer_堆积": my["堆积"], "myer_风险数字": my["risk_digit"],
            "B因子": b_hit, "M因子": m_hit,
        }
    n_ind = len(ind.INDICATORS)
    Q = 1.0 - (sum_b + sum_m) / float(2 * n_ind)
    return {
        "Q": round(Q, 4),
        "临界": {"Benford_chi2": 20.09, "末位均匀分布_chi2": 16.92},
        "判据说明": "末位堆积判据采用末位数字均匀分布 χ² 检验（df=9，5% 临界 16.92）；"
                 "Myer 指数数值照实展示但小样本下易虚高，故不作为唯一判据",
        "命中Benford数": sum_b, "命中Myer数": sum_m, "指标数": n_ind,
        "口径": "Q = 1 − (ΣB + ΣM) / (2 × 指标数)，越接近 1 数据质量越高",
        "明细": per,
        "来源": "杨贵军等，2022《统计与决策》BM-BP 双质量因子",
    }


def benford_factors(values: dict, gate: dict):
    """按第一层得到的风险数字，为单个样本构造 15 维 Benford 因子。"""
    out = []
    for key, _, _ in ind.INDICATORS:
        d = gate["明细"][key]["benford_风险数字"]
        fd = first_digit(values.get(key))
        out.append(1 if (d and fd == d) else 0)
    return out


# ---------------------------------------------------------------- 线性规划


def _simplex(c, A, b, max_iter=4000):
    """两阶段单纯形法：**maximize** cᵀx, s.t. Ax = b, x ≥ 0（要求 b ≥ 0）。

    返回 (x, obj, ok)。ok=False 表示未求得最优解（调用方应降级并如实标注）。

    ⚠ 方向说明（2026-10-28 更正）：本函数的 docstring 原写 "min cᵀx"，与实现
    不符——实现求的是 **max**。判据：c=[1,1,0]、约束 x1+x2+s=10 时返回
    obj=10（取到 x1=10），而非 minimize 下的 0。
    调用方若要求最小化目标，须自行把系数取负（见 deada 中松弛变量的处理）。
    """
    m, n = len(A), len(c)
    # 保证 b >= 0
    A = [row[:] for row in A]
    b = list(b)
    for i in range(m):
        if b[i] < 0:
            A[i] = [-v for v in A[i]]
            b[i] = -b[i]
    # Phase 1：人工变量
    N = n + m
    T = [[0.0] * (N + 1) for _ in range(m)]
    for i in range(m):
        for j in range(n):
            T[i][j] = A[i][j]
        T[i][n + i] = 1.0
        T[i][N] = b[i]
    basis = [n + i for i in range(m)]
    # Phase 1 目标：min Σ artificial
    obj1 = [0.0] * (N + 1)
    for i in range(m):
        for j in range(N + 1):
            obj1[j] -= T[i][j]
    obj1[n:] = [0.0] * (m + 1)  # 人工变量列系数置 0 后再取负
    for i in range(m):
        for j in range(N + 1):
            if j >= n:
                obj1[j] -= T[i][j]

    def pivot(T, obj, basis, phase1):
        # 【可复现性修复 2026-10-28】
        # 原实现按「最负 reduce cost」选入基、容差 1e-12。当两列的 reduce cost
        # 在浮点误差范围内接近时，不同平台的 libm / CPU / Python 版本算出的微小
        # 差异会选到不同的入基列，进而走出不同的 pivot 路径，最终导致判别得分 D
        # 跨平台不一致（实测：Windows+Py3.13 得 -0.117677，Linux+Py3.12 得
        # -0.114258，同平台内稳定但互不相同），违反「全链路可复现」要求。
        #
        # 现改为真正的 Bland 最小下标规则：
        #   入基 = 下标最小的负 reduce cost 列（与系数大小无关，只与下标有关）
        #   出基 = 最小比值行；比值并列时取基变量下标最小者
        # 下标是整数比较，不受浮点误差影响，从而保证结果跨平台一致。
        TOL = 1e-9
        for _ in range(max_iter):
            # 入基：Bland 规则 —— 下标最小的负 reduce cost
            enter = -1
            for j in range(len(obj) - 1):
                if obj[j] < -TOL:
                    enter = j
                    break
            if enter < 0:
                return True
            # 出基：最小比值；并列时取基变量下标最小者（Bland）
            leave, ratio = -1, None
            for i in range(len(T)):
                if T[i][enter] > TOL:
                    r = T[i][-1] / T[i][enter]
                    if ratio is None or r < ratio - TOL:
                        ratio, leave = r, i
                    elif abs(r - ratio) <= TOL and basis[i] < basis[leave]:
                        leave = i
            if leave < 0:
                return False  # 无界
            piv = T[leave][enter]
            T[leave] = [v / piv for v in T[leave]]
            for i in range(len(T)):
                if i != leave and abs(T[i][enter]) > TOL:
                    f = T[i][enter]
                    T[i] = [T[i][k] - f * T[leave][k] for k in range(len(T[leave]))]
            f = obj[enter]
            obj = [obj[k] - f * T[leave][k] for k in range(len(obj))]
            basis[leave] = enter
        return False

    ok1 = pivot(T, obj1, basis, True)
    if not ok1:
        return None, None, False
    # 去掉人工列；若人工变量仍在基中，用原变量替换并做行消元，保持基满秩
    T = [row[:n] + [row[-1]] for row in T]
    for i in range(m):
        if basis[i] >= n:
            replaced = False
            for j in range(n):
                if abs(T[i][j]) > 1e-9:
                    piv = T[i][j]
                    T[i] = [v / piv for v in T[i]]
                    for k in range(m):
                        if k != i and abs(T[k][j]) > 1e-9:
                            f = T[k][j]
                            T[k] = [T[k][t] - f * T[i][t] for t in range(n + 1)]
                    basis[i] = j
                    replaced = True
                    break
            if not replaced:
                return None, None, False
    obj2 = [-v for v in c] + [0.0]
    for i in range(m):
        if basis[i] < n and abs(obj2[basis[i]]) > 1e-12:
            f = obj2[basis[i]]
            obj2 = [obj2[k] - f * T[i][k] for k in range(len(obj2))]
    ok2 = pivot(T, obj2, basis, False)
    if not ok2:
        return None, None, False
    x = [0.0] * n
    for i in range(m):
        if basis[i] < n:
            x[basis[i]] = T[i][-1]
    objv = sum(c[j] * x[j] for j in range(n))
    return x, objv, True


# ---------------------------------------------------------------- 第二层


def _zscore_matrix(rows: list, keys: list):
    """按列做 z-score 标准化，消除量纲差异（X9 比率、AR_DAYS 天数量纲不同）。"""
    cols = {}
    for k in keys:
        vals = [r[k] for r in rows]
        mu = sum(vals) / float(len(vals))
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / float(len(vals))) or 1.0
        cols[k] = (mu, sd)
    out = []
    for r in rows:
        out.append({k: (r[k] - cols[k][0]) / cols[k][1] for k in keys})
    return out, cols


def deada(pool: list, target: dict):
    """第二层：改进 DEA-DA 两阶段判别，输出判别得分 D 与指标权重。"""
    keys = ind.DEADA_INPUTS + ind.DEADA_OUTPUTS
    rows = []
    labels = []
    for s in pool:
        v = dict(s["values"])
        # 前处理：投入项中「越大越差」者取负，使全部投入满足「越大越好」
        # （v8 文档 2-A.5 Step 2：逆指标需在前处理时转换）
        v["AR_DAYS"] = -ind.ar_days(v)
        v["X13"] = -v["X13"]
        rows.append(v)
        labels.append(s["label"])
    Z, norm = _zscore_matrix(rows, keys)
    # 组别方向：G1 为期望「低分」组（风险样本），约束 score + s⁺ = d → score ≤ d
    #            G2 为期望「高分」组（健康样本），约束 score − s⁻ = d → score ≥ d
    g1 = [(i, Z[i]) for i in range(len(Z)) if labels[i] == 1]
    g2 = [(i, Z[i]) for i in range(len(Z)) if labels[i] == 0]

    ni, no = len(ind.DEADA_INPUTS), len(ind.DEADA_OUTPUTS)
    nv = ni + no
    # 变量：v(ni) | u(no) | dp | dn | s+(|g1|) | s-(|g2|)
    nvar = nv + 2 + len(g1) + len(g2)
    c = [0.0] * nvar
    # 【修复 2026-10-28 · 求解方向反了】
    # _simplex 的实际语义是 **maximize cᵀx**（其 docstring 原写 "min cᵀx"，与实现
    # 不符，已一并更正）。DEA-DA 两阶段的标准目标是 **最小化** 误分类总量
    # min Σ(s⁺ + s⁻)，在 maximize 语义下必须取负号。
    # 原代码用 +1.0，等于在**最大化**所有样本的误分类：实测松弛总和高达 32.03，
    # 40 个松弛变量中 39 个非零，即几乎所有样本都被判到错误的一侧，
    # 由此求出的权重与判别得分 D 不可信。
    for k in range(nv + 2, nvar):
        c[k] = -1.0
    A, b = [], []
    for k1, (_, r) in enumerate(g1):  # ΣvX + ΣuY + s⁺ − d = 0
        row = [0.0] * nvar
        for i, k in enumerate(ind.DEADA_INPUTS):
            row[i] = r[k]
        for j, k in enumerate(ind.DEADA_OUTPUTS):
            row[ni + j] = r[k]
        row[nv] = -1.0      # dp
        row[nv + 1] = 1.0   # dn
        row[nv + 2 + k1] = 1.0
        A.append(row); b.append(0.0)
    for k2, (_, r) in enumerate(g2):  # ΣvX + ΣuY − s⁻ − d = 0
        row = [0.0] * nvar
        for i, k in enumerate(ind.DEADA_INPUTS):
            row[i] = r[k]
        for j, k in enumerate(ind.DEADA_OUTPUTS):
            row[ni + j] = r[k]
        row[nv] = -1.0
        row[nv + 1] = 1.0
        row[nv + 2 + len(g1) + k2] = -1.0
        A.append(row); b.append(0.0)
    row = [0.0] * nvar  # Σv + Σu = 1（归一化，等价于规模收益处理）
    for i in range(nv):
        row[i] = 1.0
    A.append(row); b.append(1.0)

    x, objv, ok = _simplex(c, A, b)
    degraded = False
    if not ok or x is None:
        degraded = True
        w = [1.0 / nv] * nv
        d_thr = 0.0
    else:
        w = x[:nv]
        d_thr = x[nv] - x[nv + 1]

    tv = dict(target)
    tv["AR_DAYS"] = -ind.ar_days(tv)
    tv["X13"] = -tv["X13"]
    tz = {k: (tv[k] - norm[k][0]) / norm[k][1] for k in keys}
    score = 0.0
    weights = {}
    for i, k in enumerate(ind.DEADA_INPUTS):
        weights[k] = round(w[i], 6)
        score += w[i] * tz[k]
    for j, k in enumerate(ind.DEADA_OUTPUTS):
        weights[k] = round(w[ni + j], 6)
        score += w[ni + j] * tz[k]
    D = score - d_thr  # D < 0 → 判为财务风险（与文档口径一致）
    return {
        "D": round(D, 6),
        "判别得分": round(score, 6),
        "临界值": round(d_thr, 6),
        "判为风险": D < 0,
        "权重": weights,
        "投入指标": list(ind.DEADA_INPUTS),
        "产出指标": list(ind.DEADA_OUTPUTS),
        "样本": {"健康组": len(g2), "风险组": len(g1)},
        "求解": "两阶段单纯形法（自带实现）" if not degraded else "线性规划未求得最优解，降级为等权判别",
        "降级": degraded,
        "口径": "D = (Σv·投入 + Σu·产出) − 临界值；D < 0 判为财务风险",
        "来源": "马秀颖等，2026《数理统计与管理》改进 DEA-DA（BCC 规模收益可变）",
    }


# ---------------------------------------------------------------- 第三层


class _Tree:
    def __init__(self, max_depth=4):
        self.max_depth = max_depth
        self.root = None

    @staticmethod
    def _gini(labels):
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
                l = [y[i] for i in range(len(X)) if X[i][f] <= thr]
                r = [y[i] for i in range(len(X)) if X[i][f] > thr]
                if not l or not r:
                    continue
                g = (len(l) * self._gini(l) + len(r) * self._gini(r)) / float(len(y))
                if best is None or g < best[0]:
                    best = (g, f, thr)
        if best is None:
            return {"leaf": True, "p": (sum(y) / float(len(y))) if y else 0.0}
        _, f, thr = best
        lX = [X[i] for i in range(len(X)) if X[i][f] <= thr]
        rX = [X[i] for i in range(len(X)) if X[i][f] > thr]
        ly = [y[i] for i in range(len(X)) if X[i][f] <= thr]
        ry = [y[i] for i in range(len(X)) if X[i][f] > thr]
        return {"leaf": False, "f": f, "thr": thr,
                "L": self._build(lX, ly, depth + 1),
                "R": self._build(rX, ry, depth + 1)}

    def fit(self, X, y):
        self.root = self._build(X, y, 0)
        return self

    def predict(self, x):
        node = self.root
        while node and not node["leaf"]:
            node = node["L"] if x[node["f"]] <= node["thr"] else node["R"]
        return node["p"] if node else 0.0


def benford_rf(pool: list, target: dict, gate: dict, n_trees=60, seed=20261027):
    """第三层：Benford 因子 + CART 随机森林，输出舞弊概率 P。"""
    X, y = [], []
    for s in pool:
        feats = [s["values"][k] for k, _, _ in ind.INDICATORS]
        feats += benford_factors(s["values"], gate)
        X.append(feats)
        y.append(s["label"])
    rng = random.Random(seed)
    trees = []
    n = len(X)
    for _ in range(n_trees):
        idx = [rng.randrange(n) for _ in range(n)]  # bootstrap
        bX = [X[i] for i in idx]
        by = [y[i] for i in idx]
        if len(set(by)) == 1:
            continue
        trees.append(_Tree(max_depth=4).fit(bX, by))
    tf = [target[k] for k, _, _ in ind.INDICATORS] + benford_factors(target, gate)
    votes = [t.predict(tf) for t in trees]
    P = (sum(votes) / float(len(votes))) if votes else 0.0
    return {
        "P": round(P, 4),
        "树数": len(trees),
        "特征维度": len(tf),
        "特征构成": "15 个财务比率 + 15 个 Benford 因子（共 30 维）",
        "参数": {"n_estimators": n_trees, "max_depth": 4, "bootstrap": True},
        "口径": "多数投票的舞弊概率；P ≥ 0.7 升级红灯，P < 0.3 降级黄灯",
        "来源": "孙玲莉等，2021《数量经济技术经济研究》Benford-随机森林",
    }


# ---------------------------------------------------------------- 融合
#
# 注意：融合判定的权威实现已迁移到 v8/fusion.py。
#
# 迁移原因：原实现存在「阈值符号不统一、规则用『或』连接导致彼此不互斥、
# 结果依赖 if/elif 书写顺序、空值与上游求解降级一律未处理」等问题，
# 详见 docs/融合规则口径冲突消歧报告_20261028.md。
#
# 这里保留三参数签名仅为兼容历史调用点，但**无法感知上游降级**，
# 新代码请直接调用 v8.fusion.fuse(Q, D, P, d_reliable=..., p_reliable=...)。


def fuse(Q: float, D: float, P: float):
    """兼容包装：等价于 fusion.fuse(Q, D, P)，但不感知 D/P 的上游降级。"""
    from . import fusion
    return fusion.fuse(Q, D, P)
