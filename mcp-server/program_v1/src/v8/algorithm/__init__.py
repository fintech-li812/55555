# -*- coding: utf-8 -*-
"""v8 算法包（生产版 v1.0）。

接手全部算法代码后的生产实现，替代 ``algo/RA_NEXT`` 的研究目录与
``v8/ra_next.py`` 的临时适配层。

与旧实现的关系
--------------
* **数值口径**：继承 ra-next-0.1（Q：MC 精确检验 + Holm；D：基线 DEA-DA 点估计；
  P：随机子空间森林 + 独立留出集 sigmoid 校准）。三层的历史锚点保持不变。
* **结构**：训练/打分分离、池级模型缓存、独立对偶最优性证书、
  统一校验层、证据只升不降、结构化日志。
* **依赖**：只依赖标准库与本包内部相对导入，**不依赖 algo/ 目录，
  不依赖研究目录的目录结构**，可以在任意位置被 import。

用法
----
::

    from v8.algorithm import assess
    result = assess(pool, target)                      # 完整评估
    result = assess(pool, target, with_stability=True) # 附 D 稳定性
    result = assess(pool, target, benford_applicable=True)  # Q 适用性已论证

模块职责
--------
===================  ==========================================================
validation           输入校验与规范化（参数错误抛异常，数据问题返回不可用）
simplex              标准型 LP：逐位继承基线的 Bland 单纯形 + 独立对偶证书
quality              第一层 Q：数字分布 MC 检验 + Holm 校正
deada                第二层 D：DEA-DA 训练/打分 + 稳定性
forest               第三层 P：随机子空间森林
calibration          概率校准：正则 sigmoid（a≥0 保单调）
policy               证据等级、灯色只升不降、动态阈值、多引擎统一视图
model                编排：池级模型缓存 + 全链路组装
===================  ==========================================================

契约
----
* 参数非法 → ``ValidationError``（调用方缺陷，必须修调用方）
* 数据问题 → 返回体带 ``available=False`` + ``reason``，不抛异常
* 所有对外返回体都是 JSON 可序列化的 dict，不含自定义对象
"""
from __future__ import annotations

from . import (calibration, deada, forest, model, policy, quality, simplex,
               validation)
from .model import (VERSION, assess, cache_stats, clear_cache, get_pool_model,
                    set_cache_enabled, set_cache_max)
from .policy import threshold_policy, unified_view, evidence_levels, overall_evidence

__all__ = [
    "VERSION",
    "assess",
    "get_pool_model",
    "cache_stats",
    "clear_cache",
    "set_cache_enabled",
    "set_cache_max",
    "evidence_levels",
    "overall_evidence",
    "threshold_policy",
    "unified_view",
    "calibration",
    "deada",
    "forest",
    "model",
    "policy",
    "quality",
    "simplex",
    "validation",
]
