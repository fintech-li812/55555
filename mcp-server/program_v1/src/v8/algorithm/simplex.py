# -*- coding: utf-8 -*-
"""标准型线性规划求解与最优性交叉验证。

问题形式：**maximize** cᵀx, s.t. Ax = b, x ≥ 0。

两套实现并存，是刻意的设计
--------------------------
1. ``simplex_max`` —— 与 ``v8/layers.py:_simplex`` **逐位一致**的搬运版本。
   保留它是为了保证 DEA-DA 判别得分 D 的三个锚点（0.868543 / 0.187676 /
   −0.471113）不因换求解器而漂移。它历经 2026-10-28 的 Bland 规则修复与
   跨平台验证，pivot 路径已被确认可复现，**不得改动其算术与选主规则**。

2. ``independent_solve`` —— 独立实现的两阶段 Bland 单纯形，附带
   **原始/对偶数值最优性证书**。它走的是另一条 pivot 路径，用来对
   ``simplex_max`` 的结果做**交叉验证**：两条路径给出同一目标值，
   且独立路径有对偶证书，则可确认该目标值是最优值。

为什么用「目标值相等」而不是直接验证 simplex_max 的解
------------------------------------------------------
DEA-DA 的归一化约束 Σv+Σu=1 下常出现**多重最优解**：不同的最优顶点给出
不同的权重向量，但目标值相同。因此：
* 目标值相等 + 独立路径有对偶证书 ⇒ simplex_max 的解也是最优解；
* 但**权重向量本身不可识别**，这一点必须由上层如实标注（见 deada.py）。

证书是**数值**证书（相对容差），不是有理数精确证明。
"""
from __future__ import annotations

import math

# 选主容差：与基线保持一致，保证 pivot 路径可复现
PIVOT_TOL = 1e-9
# 证书容差：相对残差 / 对偶违规 / 对偶间隙的判定门槛
CERT_TOL = 1e-7


# ---------------------------------------------------------------- 求解器一：与基线逐位一致


def simplex_max(c, A, b, max_iter=4000):
    """两阶段单纯形法：maximize cᵀx, s.t. Ax=b, x≥0。返回 (x, obj, ok)。

    本函数是 v8/layers.py:_simplex 的搬运版本，算术与选主规则完全一致：
    Bland 最小下标入基 + 最小比值出基 + 并列取基变量下标最小者。
    ok=False 表示未求得最优解，调用方必须降级并如实标注。
    """
    m, n = len(A), len(c)
    A = [row[:] for row in A]
    b = list(b)
    for i in range(m):
        if b[i] < 0:
            A[i] = [-v for v in A[i]]
            b[i] = -b[i]
    N = n + m
    T = [[0.0] * (N + 1) for _ in range(m)]
    for i in range(m):
        for j in range(n):
            T[i][j] = A[i][j]
        T[i][n + i] = 1.0
        T[i][N] = b[i]
    basis = [n + i for i in range(m)]

    obj1 = [0.0] * (N + 1)
    for i in range(m):
        for j in range(N + 1):
            obj1[j] -= T[i][j]
    obj1[n:] = [0.0] * (m + 1)
    for i in range(m):
        for j in range(N + 1):
            if j >= n:
                obj1[j] -= T[i][j]

    def pivot(T, obj, basis):
        for _ in range(max_iter):
            enter = -1
            for j in range(len(obj) - 1):
                if obj[j] < -PIVOT_TOL:
                    enter = j
                    break
            if enter < 0:
                return T, obj, basis, True
            leave, ratio = -1, None
            for i in range(len(T)):
                if T[i][enter] > PIVOT_TOL:
                    r = T[i][-1] / T[i][enter]
                    if ratio is None or r < ratio - PIVOT_TOL:
                        ratio, leave = r, i
                    elif abs(r - ratio) <= PIVOT_TOL and basis[i] < basis[leave]:
                        leave = i
            if leave < 0:
                return T, obj, basis, False
            piv = T[leave][enter]
            T[leave] = [v / piv for v in T[leave]]
            for i in range(len(T)):
                if i != leave and abs(T[i][enter]) > PIVOT_TOL:
                    f = T[i][enter]
                    T[i] = [T[i][k] - f * T[leave][k] for k in range(len(T[leave]))]
            f = obj[enter]
            obj = [obj[k] - f * T[leave][k] for k in range(len(obj))]
            basis[leave] = enter
        return T, obj, basis, False

    T, obj1, basis, ok1 = pivot(T, obj1, basis)
    if not ok1:
        return None, None, False
    T = [row[:n] + [row[-1]] for row in T]
    for i in range(m):
        if basis[i] >= n:
            replaced = False
            for j in range(n):
                if abs(T[i][j]) > PIVOT_TOL:
                    piv = T[i][j]
                    T[i] = [v / piv for v in T[i]]
                    for k in range(m):
                        if k != i and abs(T[k][j]) > PIVOT_TOL:
                            f = T[k][j]
                            T[k] = [T[k][t] - f * T[i][t] for t in range(n + 1)]
                    basis[i] = j
                    replaced = True
                    break
            if not replaced:
                return None, None, False
    obj2 = [-v for v in c] + [0.0]
    for i in range(m):
        if basis[i] < n and abs(obj2[basis[i]]) > PIVOT_TOL:
            f = obj2[basis[i]]
            obj2 = [obj2[k] - f * T[i][k] for k in range(len(obj2))]
    T, obj2, basis, ok2 = pivot(T, obj2, basis)
    if not ok2:
        return None, None, False
    x = [0.0] * n
    for i in range(m):
        if basis[i] < n:
            x[basis[i]] = T[i][-1]
    objv = sum(c[j] * x[j] for j in range(n))
    return x, objv, True


# ---------------------------------------------------------------- 求解器二：独立实现 + 对偶证书


def _gauss_solve(matrix, rhs, tol=1e-12):
    """部分主元高斯消元解方阵方程，奇异时返回 None。"""
    n = len(rhs)
    if n == 0:
        return []
    t = [list(row) + [v] for row, rhs_v in zip(matrix, rhs) for v in [rhs_v]]
    for j in range(n):
        i = max(range(j, n), key=lambda i: abs(t[i][j]))
        if abs(t[i][j]) < tol:
            return None
        t[j], t[i] = t[i], t[j]
        piv = t[j][j]
        t[j] = [v / piv for v in t[j]]
        for k in range(n):
            if k != j:
                f = t[k][j]
                if f:
                    t[k] = [v - f * w for v, w in zip(t[k], t[j])]
    return [row[-1] for row in t]


def _reduced(cost, T, basis, ncols):
    """按当前基计算约化成本：cost[j] − Σ_i cost[basis[i]]·T[i][j]。

    返回长度 ncols+1：前 ncols 个是变量列的约化成本，
    末位是右端项（成本恒为 0，仅随之消元，用于判断目标值变化）。
    """
    out = [0.0] * (ncols + 1)
    for j in range(min(len(cost), ncols)):
        out[j] = cost[j]
    for i in range(len(T)):
        b = basis[i]
        f = cost[b] if 0 <= b < len(cost) else 0.0
        if f:
            for j in range(ncols + 1):
                out[j] -= f * T[i][j]
    return out


def _pivot_step(T, obj, basis, ncols, max_iter):
    """Bland 规则迭代：入基取下标最小的正约化成本列，出基取最小比值、
    并列取基变量下标最小者。返回 (T, obj, basis, 状态)。"""
    for _ in range(max_iter):
        enter = -1
        for j in range(ncols):
            if obj[j] > PIVOT_TOL:
                enter = j
                break
        if enter < 0:
            return T, obj, basis, "optimal"
        leave, ratio = -1, None
        for i in range(len(T)):
            if T[i][enter] > PIVOT_TOL:
                r = T[i][-1] / T[i][enter]
                if ratio is None or r < ratio - PIVOT_TOL:
                    ratio, leave = r, i
                elif abs(r - ratio) <= PIVOT_TOL and basis[i] < basis[leave]:
                    leave = i
        if leave < 0:
            return T, obj, basis, "unbounded"
        piv = T[leave][enter]
        T[leave] = [v / piv for v in T[leave]]
        for i in range(len(T)):
            if i != leave:
                f = T[i][enter]
                if abs(f) > PIVOT_TOL:
                    T[i] = [T[i][k] - f * T[leave][k] for k in range(len(T[leave]))]
        f = obj[enter]
        obj = [obj[k] - f * T[leave][k] for k in range(len(obj))]
        basis[leave] = enter
    return T, obj, basis, "iteration_limit"


def independent_solve(c, A, b, max_iter=10000):
    """独立两阶段 Bland 单纯形，返回 (x, obj, diagnostics)。

    diagnostics 里含对偶证书。与 ``simplex_max`` 走不同的 pivot 路径，
    用于交叉验证。
    """
    n, m = len(c), len(b)
    diag = {"solved": False, "optimality_verified": False}
    if not n or not m or len(A) != m or any(len(row) != n for row in A):
        diag["reason"] = "LP 维度不匹配"
        return None, None, diag
    try:
        flat = list(c) + list(b) + [v for row in A for v in row]
        if any((isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(float(v))) for v in flat):
            diag["reason"] = "LP 系数含非有限值"
            return None, None, diag
    except TypeError:
        diag["reason"] = "LP 系数类型非法"
        return None, None, diag

    c = [float(v) for v in c]
    b = [float(v) for v in b]
    A = [[float(v) for v in row] for row in A]

    # 行符号归一，保证 b ≥ 0；记录符号以便还原对偶变量
    signs = [1.0 if rhs >= 0 else -1.0 for rhs in b]
    aa = [[v * signs[i] for v in row] for i, row in enumerate(A)]
    bb = [rhs * signs[i] for i, rhs in enumerate(b)]

    T = [list(row) + [1.0 if i == j else 0.0 for j in range(m)] + [bb[i]]
         for i, row in enumerate(aa)]
    basis = [n + i for i in range(m)]
    N = n + m

    # 阶段一：maximize −Σ 人工变量
    cost1 = [0.0] * n + [-1.0] * m
    obj1 = _reduced(cost1, T, basis, N)
    T, obj1, basis, status = _pivot_step(T, obj1, basis, N, max_iter)
    artificial = sum(T[i][-1] for i in range(len(T)) if basis[i] >= n)
    if status != "optimal" or artificial > 1e-7:
        diag["reason"] = ("不可行：%s" % status) if status != "optimal" else "不可行"
        diag["artificial_sum"] = artificial
        return None, None, diag

    # 剔除人工变量仍留在基中的行；无可换出列则该行为冗余行，直接删除
    active = list(range(m))
    for i in range(len(T) - 1, -1, -1):
        if basis[i] >= n:
            j = next((j for j in range(n) if j not in basis
                      and abs(T[i][j]) > PIVOT_TOL), None)
            if j is not None:
                piv = T[i][j]
                T[i] = [v / piv for v in T[i]]
                for k in range(len(T)):
                    if k != i:
                        f = T[k][j]
                        if abs(f) > PIVOT_TOL:
                            T[k] = [T[k][t] - f * T[i][t] for t in range(N + 1)]
                basis[i] = j
            elif abs(T[i][-1]) <= 1e-7:
                del T[i]
                del basis[i]
                del active[i]
            else:
                diag["reason"] = "不可行（冗余行右端非零）"
                return None, None, diag

    # 阶段二：最大化原目标
    T = [row[:n] + [row[-1]] for row in T]
    obj2 = _reduced(list(c), T, basis, n)
    T, obj2, basis, status = _pivot_step(T, obj2, basis, n, max_iter)
    if status != "optimal":
        diag["reason"] = status
        return None, None, diag

    x = [0.0] * n
    for i, j in enumerate(basis):
        x[j] = T[i][-1]
    objv = sum(ci * xi for ci, xi in zip(c, x))

    # 对偶：解 Bᵀy = c_B（B 为原始矩阵中基变量对应的列）
    bt = [[aa[i][j] for i in active] for j in basis]
    yy = _gauss_solve(bt, [c[j] for j in basis])
    if yy is None:
        diag["reason"] = "对偶基奇异，无法出证书"
        return x, objv, diag
    y = [0.0] * m
    for i, v in zip(active, yy):
        y[i] = v * signs[i]

    cert = certificate(c, A, b, x, y)
    diag.update(cert)
    diag["solved"] = True
    diag["pivots"] = len(basis)
    return x, objv, diag


def certificate(c, A, b, x, y, tol=CERT_TOL):
    """原始/对偶最优性证书（数值，非精确算术）。

    由弱对偶性：原始可行 + 对偶可行 + 目标值相等 ⇒ 最优。
    """
    if len(x) != len(c) or len(y) != len(b):
        return {"optimality_verified": False, "reason": "证书维度不匹配"}
    if any(not math.isfinite(float(v)) for v in list(x) + list(y)):
        return {"optimality_verified": False, "reason": "证书含非有限值"}
    primal = max((abs(sum(a * v for a, v in zip(row, x)) - rhs)
                  / (1 + abs(rhs) + sum(abs(a * v) for a, v in zip(row, x)))
                  for row, rhs in zip(A, b)), default=0.0)
    dual = 0.0
    for j, cj in enumerate(c):
        slack = sum(row[j] * yi for row, yi in zip(A, y)) - cj
        scale = 1 + abs(cj) + sum(abs(row[j] * yi) for row, yi in zip(A, y))
        dual = max(dual, max(0.0, -slack) / scale)
    po = sum(ci * xi for ci, xi in zip(c, x))
    do = sum(bi * yi for bi, yi in zip(b, y))
    gap = abs(po - do) / (1 + abs(po) + abs(do))
    negative = max(0.0, -min(x, default=0.0))
    worst = max(primal, dual, gap, negative)
    return {
        "optimality_verified": worst <= tol,
        "relative_primal_residual": primal,
        "relative_dual_violation": dual,
        "relative_duality_gap": gap,
        "negative_variable": negative,
        "tolerance": tol,
        "primal_objective": po,
        "dual_objective": do,
        "reason": ("原始可行 + 对偶可行 + 目标相等 ⇒ 最优（数值容差 %g）" % tol
                   if worst <= tol else "违反约束或对偶间隙过大（最坏项 %g）" % worst),
    }


# ---------------------------------------------------------------- 对外：交叉验证求解


def solve_verified(c, A, b, *, max_iter=4000, tol=CERT_TOL, verify=True):
    """求解并在需要时做对偶交叉验证。返回 (x, obj, ok, diagnostics)。

    优先采用 ``simplex_max`` 的解（保证 D 锚点不漂移），
    再用 ``independent_solve`` 独立求一遍，仅当**目标值一致且独立路径有证书**
    时才把 ``optimality_verified`` 置为真。
    """
    x, obj, ok = simplex_max(c, A, b, max_iter)
    diag = {"solver": "bland_simplex（与基线一致）", "optimality_verified": False}
    if not ok or x is None:
        # 基线路径在「冗余约束且右端为 0」时会直接判失败：人工变量留在基里
        # 但找不到可换入的原变量，于是整题被判为无解。
        # 实测：max x, s.t. x=1, 2x=2 → 基线路返回失败，而题目显然有解 x=1。
        # 这里让独立求解器接管，且**必须持有对偶证书才采纳**，
        # 并在返回体里标明来源，绝不假装是基线路解出来的。
        if not verify:
            diag.update(solved=False,
                        reason="单纯形未求得最优解（不可行 / 无界 / 迭代上限 / 冗余约束）")
            return None, None, False, diag
        ix, iobj, idiag = independent_solve(c, A, b)
        if not idiag.get("solved") or not idiag.get("optimality_verified"):
            diag.update(solved=False,
                        reason="单纯形未求得最优解；备选路径："
                               + (idiag.get("reason") or "未取得对偶证书"))
            return None, None, False, diag
        diag.update(idiag)
        diag["solver"] = "independent_fallback（基线路径未求出解，已标注）"
        diag["solved"] = True
        return ix, iobj, True, diag
    if any(not math.isfinite(v) for v in x) or not math.isfinite(obj):
        diag.update(solved=False, reason="解含非有限值")
        return None, None, False, diag

    residual = max((abs(sum(a * v for a, v in zip(row, x)) - rhs)
                    / (1 + abs(rhs) + sum(abs(a * v) for a, v in zip(row, x)))
                    for row, rhs in zip(A, b)), default=0.0)
    neg = max(0.0, -min(x, default=0.0))
    diag.update(solved=True, relative_primal_residual=residual, negative_variable=neg)
    if residual > CERT_TOL or neg > CERT_TOL:
        diag["reason"] = "解违反原始约束或非负性，不可信"
        return None, None, False, diag
    if not verify:
        diag["reason"] = "未启用对偶交叉验证（verify=False）"
        return x, obj, True, diag

    ix, iobj, idiag = independent_solve(c, A, b)
    diag["independent"] = {
        "solved": idiag.get("solved", False),
        "verified": idiag.get("optimality_verified", False),
        "objective": iobj,
        "reason": idiag.get("reason", ""),
    }
    if not idiag.get("solved") or not idiag.get("optimality_verified"):
        diag["reason"] = "独立求解未取得对偶证书，最优性未确认"
        return x, obj, True, diag
    if abs(iobj - obj) > CERT_TOL * (1 + abs(obj)):
        diag["objective_gap"] = abs(iobj - obj)
        diag["reason"] = "两条路径目标值不一致，最优性未确认"
        return x, obj, True, diag

    diag["optimality_verified"] = True
    diag["objective_gap"] = abs(iobj - obj)
    diag["reason"] = "独立路径目标值一致且持有对偶证书 ⇒ 最优（数值容差）"
    return x, obj, True, diag
