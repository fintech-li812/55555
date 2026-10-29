# -*- coding: utf-8 -*-
"""编排层：训练与打分分离 + 模型缓存 + 全链路组装。

为什么必须分离
--------------
原实现每来一个请求就：
    重建样本池 → 重算 30 项 MC 检验 → 重解一次 LP → 重训 60 棵树 → 重跑校准
而这四步**全都只依赖样本池**，与受评对象无关。
实测单个请求 0.26 秒里有 99% 是这些池级计算。

分离后：池级产物缓存复用，请求只做 60 次树遍历 + 一次矩阵乘，
这是本次性能优化的主体（见文档中的实测对比）。

并发说明
--------
服务是 ThreadingHTTPServer。缓存构建**刻意不加全局锁**：
构建是输入确定的纯函数，并发重复构建只会浪费一次 CPU，
不会产出不一致结果。加锁反而会让 5 个并发请求串行等 1.3 秒。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from .. import fusion
from . import deada, forest, policy, quality
from .validation import (N_TREES_RANGE, ValidationError, float_param, int_param,
                         target_signature, value_problems)

LOGGER = logging.getLogger("v8.algorithm.model")

VERSION = "ra-next-v1.0"
QUALITY_SEED = 20261029
FOREST_SEED = 20261027
DEADA_SEED = 20261029

N_TREES = 60
ALPHA = 0.05
SIMULATIONS = 1999

_CACHE_MAX = 64
_CACHE_ENABLED = True      # 由 config.cache.enabled 驱动；关掉只影响耗时，不影响结果
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
# 每个缓存 key 一把构建锁。只在缓存缺失时创建，构建完成即移除，
# 因此规模受限于「正在构建中的 key 数」，不会无限增长。
_BUILD_LOCKS = {}
_STATS = {"hits": 0, "misses": 0, "evictions": 0, "bypassed": 0}


class PoolModel:
    """样本池级产物，构造完成后视为只读。"""

    __slots__ = ("fingerprint", "quality", "dead", "dead_diag", "forest_model",
                 "calibration", "build_seconds", "pool_size")

    def __init__(self, fingerprint, quality_result, dead, dead_diag,
                 forest_model, calibration, build_seconds, pool_size):
        self.fingerprint = fingerprint
        self.quality = quality_result
        self.dead = dead
        self.dead_diag = dead_diag
        self.forest_model = forest_model
        self.calibration = calibration
        self.build_seconds = build_seconds
        self.pool_size = pool_size


def _build_pool_model(pool, *, n_trees, alpha, simulations, verify) -> PoolModel:
    started = time.perf_counter()
    fingerprint = "%s|t%d|a%.4f|s%d|v%d" % (
        _signature(pool), n_trees, alpha, simulations, int(verify))
    q = quality.quality_gate(pool, alpha=alpha, simulations=simulations,
                             seed=QUALITY_SEED)
    dead_model, dead_diag = deada.fit(pool, verify=verify)
    fmodel, calib, _reason = forest.train(pool, n_trees=n_trees, seed=FOREST_SEED)
    elapsed = time.perf_counter() - started
    LOGGER.info("构建池级模型：指纹=%s 池=%d Q=%s 最优性=%s 树=%s 耗时=%.3fs",
                fingerprint, len(pool), q.get("Q"),
                dead_diag.get("optimality_verified"),
                len(fmodel.trees) if fmodel else 0, elapsed)
    return PoolModel(fingerprint, q, dead_model, dead_diag, fmodel, calib,
                     elapsed, len(pool))


def _signature(pool) -> str:
    from .validation import pool_signature
    return pool_signature(pool)


def get_pool_model(pool, *, n_trees: int = N_TREES, alpha: float = ALPHA,
                   simulations: int = SIMULATIONS, verify: bool = True) -> PoolModel:
    """取（或构建）样本池级模型。键含全部影响结果的训练参数。

    缓存可由 ``config.cache.enabled`` 关闭：关掉后每次调用都重建，
    行为与加缓存之前逐位一致（结果与是否命中缓存无关，缓存只影响耗时）。
    """
    n_trees = int_param(n_trees, "n_trees", *N_TREES_RANGE)
    alpha = float_param(alpha, "alpha", 0.0, 1.0, lo_open=True, hi_open=True)
    simulations = int_param(simulations, "simulations", 99, 20000)

    if not _CACHE_ENABLED:
        _STATS["bypassed"] += 1
        return _build_pool_model(pool, n_trees=n_trees, alpha=alpha,
                                 simulations=simulations, verify=verify)

    key = (_signature(pool), n_trees, alpha, simulations, verify)

    # 取（或建）这个 key 专属的构建锁。**一个 key 一把锁，不是全局一把锁**：
    # 全局锁会让不同样本池的请求串行等待，而它们的构建互不相关。
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            _STATS["hits"] += 1
            return cached
        lock = _BUILD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BUILD_LOCKS[key] = lock
        _STATS["misses"] += 1

    # 为什么这里必须有锁（此前「刻意不加锁」是错的）：
    # 构建是纯函数，并发重复构建不会产出不一致结果，这点没错；
    # 但 8 个线程同时做纯 Python 的 CPU 密集计算会互相抢 GIL，
    # 实测 8 并发首评 P95 从 ~1.3s 恶化到 9.7s（约 8 倍）。
    # 加 per-key 锁后变成 1 次构建 + 7 次等待，墙钟时间回到单次构建量级。
    with lock:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                _STATS["hits"] += 1
                return cached
        model = _build_pool_model(pool, n_trees=n_trees, alpha=alpha,
                                  simulations=simulations, verify=verify)
        with _CACHE_LOCK:
            if key not in _CACHE:
                if len(_CACHE) >= _CACHE_MAX:
                    _CACHE.popitem(last=False)
                    _STATS["evictions"] += 1
                _CACHE[key] = model
            _CACHE.move_to_end(key)
            # 锁已完成使命；留着会让字典无限增长（key 数量随参数组合增长）
            _BUILD_LOCKS.pop(key, None)
            return _CACHE[key]


def set_cache_enabled(enabled: bool) -> None:
    """开关：池级模型缓存。关掉即回到每请求全量重算（行为不变，只变耗时）。"""
    global _CACHE_ENABLED
    _CACHE_ENABLED = bool(enabled)
    if not _CACHE_ENABLED:
        with _CACHE_LOCK:
            _CACHE.clear()
    LOGGER.info("池级模型缓存 = %s", _CACHE_ENABLED)


def set_cache_max(n: int) -> None:
    """设置缓存条目上限（LRU）。"""
    global _CACHE_MAX
    _CACHE_MAX = max(1, int(n))
    with _CACHE_LOCK:
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    LOGGER.info("池级模型缓存上限 = %d", _CACHE_MAX)


def cache_stats() -> dict:
    with _CACHE_LOCK:
        return {"启用": _CACHE_ENABLED, "命中": _STATS["hits"],
                "未命中": _STATS["misses"], "淘汰": _STATS["evictions"],
                "绕过（缓存关闭）": _STATS["bypassed"],
                "当前条目": len(_CACHE), "上限": _CACHE_MAX}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _BUILD_LOCKS.clear()
    LOGGER.info("算法模型缓存已清空")


# ---------------------------------------------------------------- 对外主入口


def assess(pool, target, *, n_trees: int = N_TREES, alpha: float = ALPHA,
           simulations: int = SIMULATIONS, with_stability: bool = False,
           benford_applicable=None, verify: bool = True,
           require_full_evidence_for_green: bool = True,
           q_in_fusion: bool = True):
    """一次完整评估。

    ``benford_applicable`` 三态语义（**这是本次定下的口径，务必按此使用**）
    --------------------------------------------------------------------
    * ``None``（默认）：Q 数值照常进入融合表，证据等级标为「不足」——
      Benford 对有界比率的适用性未确认，但**不因此把 Q 从决策里摘掉**。
    * ``True``：Q 进入融合表，证据等级「充分」。仅在适用性已被论证后使用。
    * ``False``：Q 不进入融合表（融合层按缺失处理），证据等级「缺失」。
      融合表对此有既定规则（R1），后果是红灯，调用方须清楚自己在做什么。

    ``q_in_fusion=False``（Q 降为数据质量报告）
    ----------------------------------------
    Q 数值照算、照常返回，但**不参与融合判别也不参与证据短板**。
    用于业务决定「第一层只作数据质量报告」的场合（Q 实测区间 [0.93,1.0]
    而融合阈值是 0.3/0.5，旧尺度遗留使第一层几乎不起作用）。
    默认 ``True``，即保持原有逻辑。

    刻意不采纳的做法
    ----------------
    ra-next-0.3 默认把 Q 当缺失送进融合，导致**所有企业一律红灯**；
    又把 P 校准不足当成「P 不可用」，导致最危险样本从红灯降到黄灯。
    本版两条都不采纳：证据不足只做**上档与人工复核标记**，从不降档。
    """
    if benford_applicable not in (None, True, False):
        raise ValidationError("benford_applicable 必须是 None / True / False",
                              field="benford_applicable")
    if not isinstance(q_in_fusion, bool):
        raise ValidationError("q_in_fusion 必须是 bool", field="q_in_fusion")
    if not isinstance(pool, (list, tuple)) or not pool:
        return _failed("样本池为空")
    if not isinstance(target, dict):
        return _failed("受评对象必须是 dict")
    # 允许调用方直接传 TARGET_SCENARIOS[k] 这种带 values 的包装对象，
    # 也允许传 15 个比率的裸字典。不统一会造成「看起来跑通、实则全不可用」。
    if isinstance(target.get("values"), dict):
        target = target["values"]
    problems = value_problems(target)
    if problems:
        return _failed("受评对象：" + problems[0])

    started = time.perf_counter()
    model = get_pool_model(pool, n_trees=n_trees, alpha=alpha,
                           simulations=simulations, verify=verify)

    # ---- 第一层
    q_result = dict(model.quality)
    if benford_applicable is None:
        q_result["适用性声明"] = "未声明（证据等级记为不足，但数值照常进入融合表）"
    elif benford_applicable:
        q_result["适用性声明"] = "已声明适用"
    else:
        q_result["适用性声明"] = "已声明不适用（Q 不进入融合表）"
    q_value = q_result.get("Q") if benford_applicable is not False else None

    # ---- 第二层
    if model.dead is None:
        d_value, d_detail = None, {"D": None, "available": False,
                                   "reason": model.dead_diag.get("reason", "训练失败"),
                                   "最优性确认": False}
    else:
        d_value, d_detail = deada.score(model.dead, target)
        if d_value is None:
            d_detail = {"D": None, "available": False, "reason": d_detail,
                        "最优性确认": model.dead_diag.get("optimality_verified", False)}
        else:
            d_detail = dict(d_detail, available=True)

    # ---- 第三层
    if model.forest_model is None:
        p_detail = {"P": None, "available": False, "树数": 0,
                    "reason": "森林未训练成功"}
    else:
        p_detail = forest.score(model.forest_model, model.calibration, target,
                                n_trees=n_trees)
    p_value = p_detail.get("P")

    # ---- 融合（48 格表是唯一权威，不复制、不改写）
    # 可用性 = 有无可信数值；证据是否充分是**另一回事**，由证据层单独处理。
    fused = fusion.fuse(q_value, d_value, p_value,
                        d_reliable=d_value is not None,
                        p_reliable=p_value is not None,
                        q_in_fusion=q_in_fusion)

    levels, skipped = policy.evidence_levels(
        q_result, d_detail, p_detail,
        benford_applicable=benford_applicable, q_in_fusion=q_in_fusion)
    level, review, note = policy.apply_evidence(
        fused["等级"], levels,
        require_full_evidence_for_green=require_full_evidence_for_green,
        skip=skipped)
    overall = policy.overall_evidence(levels, skipped)

    result = {
        "算法版本": VERSION,
        "模型指纹": model.fingerprint,
        "第一层": q_result,
        "第二层": d_detail,
        "第三层": p_detail,
        "融合判定": fused,
        "证据": {
            "各层": levels,
            "整体": overall,
            "不参与短板的层": sorted(skipped),
            "复核必需": review,
            "说明": note,
            "Q参与判别": bool(q_in_fusion),
        },
        "最终等级": {
            "等级": level,
            "规则": fused["规则ID"],
            "依据": fused.get("依据", ""),
            "证据调整": note,
        },
        "声明": {
            "数据": "训练与演示数据为固定种子合成数据，不代表任何真实企业",
            "Q 含义": "样本池数字分布一致性；不是受评对象造假证据，也不是舞弊概率",
            "P 含义": "合成风险标签下的概率，不是真实舞弊概率",
            "D 含义": "DEA-DA 判别得分与临界值之差；权重不可识别，不作指标重要性解释",
            "融合口径": "沿用 fuse-v2.0 48 格决策表，本版未改动任何一格",
            "证据口径": "证据不足只上档与标记复核，从不降档",
            "Q参与判别": ("Q 参与融合判别（默认）" if q_in_fusion else
                        "Q 仅作数据质量报告，不参与融合判别与证据短板"),
        },
        "耗时": {
            "本次打分": round(time.perf_counter() - started, 6),
            "池级模型构建": round(model.build_seconds, 6),
        },
    }

    if with_stability:
        target_sig = target_signature(target)
        result["稳定性"] = _stability_cached(pool, target, target_sig)

    LOGGER.info("assess 完成：Q=%s D=%s P=%s 规则=%s 等级=%s 证据=%s 耗时=%.4fs",
                q_value, d_value, p_value, fused["规则ID"], level, overall,
                result["耗时"]["本次打分"])
    return result


# ---------------------------------------------------------------- 内部


_STABILITY_CACHE = OrderedDict()
_STABILITY_LOCK = threading.Lock()
_STABILITY_MAX = 32


def _stability_cached(pool, target, target_sig: str) -> dict:
    key = (_signature(pool), target_sig)
    with _STABILITY_LOCK:
        cached = _STABILITY_CACHE.get(key)
        if cached is not None:
            return cached
    out = deada.stability(pool, target)
    with _STABILITY_LOCK:
        if key not in _STABILITY_CACHE:
            if len(_STABILITY_CACHE) >= _STABILITY_MAX:
                _STABILITY_CACHE.popitem(last=False)
            _STABILITY_CACHE[key] = out
        _STABILITY_CACHE.move_to_end(key)
        return _STABILITY_CACHE[key]


def _failed(reason: str) -> dict:
    LOGGER.info("assess 输入不可用：%s", reason)
    return {
        "算法版本": VERSION,
        "available": False,
        "reason": reason,
        "第一层": {"Q": None, "available": False, "reason": reason},
        "第二层": {"D": None, "available": False, "reason": reason},
        "第三层": {"P": None, "available": False, "reason": reason, "树数": 0},
        "融合判定": fusion.fuse(None, None, None, d_reliable=False, p_reliable=False),
    }
