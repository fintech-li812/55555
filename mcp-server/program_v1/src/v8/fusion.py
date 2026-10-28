# -*- coding: utf-8 -*-
"""三层融合判定 FUSE(Q, D, P) —— 消歧后的唯一权威实现。

背景
----
v8 原实现（``v8/layers.py:fuse``）与方案文档之间存在多处口径冲突，
且规则用「或」连接、彼此不互斥，结果依赖 ``if/elif`` 的书写顺序；
空值、NaN、越界、上游求解降级一律未处理。冲突清单与消歧理由见
``docs/融合规则口径冲突消歧报告_20261028.md``。

本模块把这些冲突统一成一张**互斥且完备**的决策表（48 种状态组合
一一映射，无重叠、无空洞、无兜底），并把每个输入的可用性
（缺失 / 非数值 / 越界 / 上游降级）显式纳入判定，
避免把「无法判断」静默当成「无风险」。

设计约束（沿用 v8_完整方案.md，不引入无关新约束）
------------------------------------------------
* 三层仍是串行证据链：前一层的输出是后一层的输入（2-A.3）；
* 来源优先级 **Q > D > P**：数据不可信时，后两层的结论失去基础；
* 红灯仍要求「财务风险 D<0」与「舞弊证据 P≥0.7」同时成立（2-A.7 口径，
  与 2-A.3 流程图「对第二层判定为风险的样本做复核」一致）；
* 阈值符号统一为 **≥ 下界**（P=0.7 归入「高」，与 2-A.7 / 4.0 / 原代码一致，
  2-A.3 的 ``P > 0.7`` 视为笔误并更正）。

唯一的行为变化
--------------
``FUSE-R6``：``D≥0 且 P≥0.7`` 由「绿灯」改为「黄灯（结论冲突，需人工复核）」。
原实现在此情形下完全丢弃第三层输出，可以对 P=0.98 的样本判「绿灯 · 不触发动作」，
使第三层在融合中等同于死代码。新规则保留原红灯口径不变，仅不再放行绿灯。

用法
----
::

    from v8 import fusion
    fused = fusion.fuse(gate["Q"], da["D"], rf["P"],
                        d_reliable=not da.get("降级", False),
                        p_reliable=rf.get("树数", 0) > 0)
"""
from __future__ import annotations

import math

RULE_VERSION = "fuse-v2.0"

# ---------------------------------------------------------------- 阈值
# 集中定义，避免同一阈值在多处硬编码后出现漂移。
Q_BAD_MAX = 0.3      # Q < 0.3           → 数据质量严重不足
Q_OK_MIN = 0.5       # Q ≥ 0.5           → 质量达标（绿灯门槛）
P_HIGH_MIN = 0.7     # P ≥ 0.7           → 舞弊概率高
P_LOW_MAX = 0.3      # P < 0.3           → 排除粉饰

_EPS = 1e-9          # 仅用于合法性区间判断，不用于阈值比较

# ---------------------------------------------------------------- 状态枚举
# 术语统一：每个输入先离散化为有序状态，规则只针对状态书写，
# 从而消除「区间边界怎么算」「两个阈值之间算什么」这类歧义。

# 第一层：数据质量
Q_MISSING = "缺失"        # 空值 / 非数值 / 越界
Q_BAD = "严重不足"        # Q < 0.3
Q_WARN = "存疑"           # 0.3 ≤ Q < 0.5
Q_OK = "达标"             # Q ≥ 0.5

# 第二层：财务健康
D_MISSING = "缺失"        # 空值 / 非数值 / 上游求解降级
D_RISK = "风险"           # D < 0
D_OK = "正常"             # D ≥ 0

# 第三层：舞弊概率
P_MISSING = "缺失"        # 空值 / 非数值 / 越界 / 树数=0
P_HIGH = "高"             # P ≥ 0.7
P_MID = "中"              # 0.3 ≤ P < 0.7
P_LOW = "低"              # P < 0.3

ALL_Q = (Q_MISSING, Q_BAD, Q_WARN, Q_OK)
ALL_D = (D_MISSING, D_RISK, D_OK)
ALL_P = (P_MISSING, P_HIGH, P_MID, P_LOW)


# ---------------------------------------------------------------- 输入规范化


def _normalize(value, lo=None, hi=None):
    """把输入归一化为有限实数；不可用则返回 None。

    判定为不可用的情形：
      * ``None``；
      * 布尔值（``bool`` 是 ``int`` 的子类，但作为质量得分/概率出现必是缺陷）；
      * 字符串 / 字节串：**不做隐式转换**。Q/D/P 在本项目中一律由内部计算
        得出（``round(x, 4)``），出现字符串只可能是上游契约被破坏；
        ``float("0.8")`` 能成功，但那会把一个类型错误悄悄变成一次正常判定。
        此处与 v2 的 ``risk_model.number()`` 口径保持一致；
      * 其他无法转为 float 的类型（容器等）；
      * ``NaN`` / ``±Inf``；
      * 超出给定合法区间（含 1e-9 容差，避免浮点误差误杀边界值 1.0）。

    注意：本函数**不做**阈值比较，也不对数值做四舍五入——
    阈值比较由决策表用原始精度完成，行为可预测、可复现。
    """
    if value is None or isinstance(value, (bool, str, bytes, bytearray)):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if lo is not None and v < lo - _EPS:
        return None
    if hi is not None and v > hi + _EPS:
        return None
    return v


def _q_state(Q):
    v = _normalize(Q, 0.0, 1.0)
    if v is None:
        return Q_MISSING
    if v < Q_BAD_MAX:
        return Q_BAD
    if v < Q_OK_MIN:
        return Q_WARN
    return Q_OK


def _d_state(D, reliable=True):
    """D 的状态。``reliable=False`` 表示上游 DEA-DA 求解降级，得分不可信。"""
    if not reliable:
        return D_MISSING
    v = _normalize(D)          # D 是 z-score 的加权和，理论上无界，不做区间校验
    if v is None:
        return D_MISSING
    return D_RISK if v < 0 else D_OK


def _p_state(P, reliable=True):
    """P 的状态。``reliable=False`` 表示随机森林未训练出任何树，概率无意义。"""
    if not reliable:
        return P_MISSING
    v = _normalize(P, 0.0, 1.0)
    if v is None:
        return P_MISSING
    if v >= P_HIGH_MIN:
        return P_HIGH
    if v < P_LOW_MAX:
        return P_LOW
    return P_MID


# ---------------------------------------------------------------- 决策表
#
# 互斥性：每行是 (Q状态集合, D状态集合, P状态集合) 的笛卡尔积，
#         任意两行的三元组集合两两不相交。
# 完备性：|Q|=4 × |D|=3 × |P|=4 = 48 种组合，下表恰好覆盖 48 种（见测试）。
# 顺序无关性：因互斥，任意调换行的书写顺序，同一输入映射到的行不变。
#
# 规则按「证据充分性」从高到低排列，仅为可读性；判定不依赖此顺序。


def _r5_reason(st):
    """R5：D<0 且 P<0.7 —— 同为黄灯，但依据必须区分，否则留痕不可追溯。"""
    if st["P"] == P_LOW:
        return ("判为财务风险（D<0）；舞弊概率 P<0.3 排除粉饰，"
                "降级为黄灯（仅为经营性问题）")
    if st["P"] == P_MID:
        return "判为财务风险（D<0）；舞弊概率 0.3≤P<0.7 处于灰区，维持黄灯"
    return ("判为财务风险（D<0）；第三层复核不可用，"
            "无法排除粉饰，维持黄灯")


RULES = [
    {
        "id": "FUSE-R1",
        "q": (Q_MISSING,), "d": ALL_D, "p": ALL_P,
        "level": "红灯",
        "reason": lambda st: ("第一层数据质量得分不可用（缺失/非数值/越界），"
                              "门禁失效，无法支撑任何结论"),
    },
    {
        "id": "FUSE-R2",
        "q": (Q_BAD,), "d": ALL_D, "p": ALL_P,
        "level": "红灯",
        "reason": lambda st: ("数据质量 Q<0.3，质量严重不足；"
                              "后续两层的结论建立在不可信数据上，不再参与判定"),
    },
    {
        "id": "FUSE-R3",
        "q": (Q_WARN, Q_OK), "d": (D_MISSING,), "p": ALL_P,
        "level": "黄灯",
        "reason": lambda st: ("第二层判别得分不可用（缺失或非数值，"
                              "或 DEA-DA 求解降级），无法判定财务健康，按存疑处理"),
    },
    {
        "id": "FUSE-R4",
        "q": (Q_WARN, Q_OK), "d": (D_RISK,), "p": (P_HIGH,),
        "level": "红灯",
        "reason": lambda st: "判为财务风险（D<0）且舞弊概率 P≥0.7，风险叠加疑似粉饰",
    },
    {
        "id": "FUSE-R5",
        "q": (Q_WARN, Q_OK), "d": (D_RISK,), "p": (P_MID, P_LOW, P_MISSING),
        "level": "黄灯",
        "reason": _r5_reason,
    },
    {
        "id": "FUSE-R6",
        "q": (Q_WARN, Q_OK), "d": (D_OK,), "p": (P_HIGH,),
        "level": "黄灯",
        "reason": lambda st: ("财务健康判别 D≥0，但舞弊概率 P≥0.7，"
                              "两个结论相互冲突；不放行绿灯，转人工复核"),
    },
    {
        "id": "FUSE-R7",
        "q": (Q_WARN,), "d": (D_OK,), "p": (P_MID, P_LOW),
        "level": "黄灯",
        "reason": lambda st: ("财务健康（D≥0）且无舞弊迹象（P<0.7），"
                              "但数据质量 0.3≤Q<0.5 未达绿灯门槛 Q≥0.5"),
    },
    {
        "id": "FUSE-R8",
        "q": (Q_WARN,), "d": (D_OK,), "p": (P_MISSING,),
        "level": "黄灯",
        "reason": lambda st: ("数据质量 0.3≤Q<0.5 未达绿灯门槛，"
                              "且第三层复核不可用，证据链不完整"),
    },
    {
        "id": "FUSE-R9",
        "q": (Q_OK,), "d": (D_OK,), "p": (P_MISSING,),
        "level": "黄灯",
        "reason": lambda st: ("数据质量与财务健康均达标，"
                              "但第三层复核不可用，证据链不完整，不放行绿灯"),
    },
    {
        "id": "FUSE-R10",
        "q": (Q_OK,), "d": (D_OK,), "p": (P_MID, P_LOW),
        "level": "绿灯",
        "reason": lambda st: ("数据质量 Q≥0.5、财务健康 D≥0、"
                              "舞弊概率 P<0.7，三层证据一致支持健康"),
    },
]

RULE_TEXT = (
    "红灯：Q不可用，或 Q<0.3，或 (D<0 且 P≥0.7)；"
    "黄灯：D不可用，或 (D<0 且 P<0.7)，或 (D≥0 且 P≥0.7 结论冲突)，"
    "或 (D≥0 且 Q<0.5)，或 (D≥0 且 Q≥0.5 且 P不可用)；"
    "绿灯：Q≥0.5 且 D≥0 且 P<0.7"
)


def _match(states):
    """返回唯一命中的规则；未命中说明决策表有洞，属实现缺陷，必须显式报错。"""
    for rule in RULES:
        if (states["Q"] in rule["q"]
                and states["D"] in rule["d"]
                and states["P"] in rule["p"]):
            return rule
    raise RuntimeError(
        "融合决策表未覆盖状态组合 Q=%s D=%s P=%s，属实现缺陷，"
        "请补全 RULES 而不是加兜底分支" % (states["Q"], states["D"], states["P"]))


# ---------------------------------------------------------------- 对外接口


def fuse(Q, D, P, *, d_reliable=True, p_reliable=True):
    """三层融合判定 FUSE(Q, D, P)。

    参数
    ----
    Q : 数据质量得分，合法区间 [0, 1]
    D : 财务健康判别得分，实数；``d_reliable=False`` 表示上游求解降级、得分不可信
    P : 舞弊概率，合法区间 [0, 1]；``p_reliable=False`` 表示随机森林未训练出树

    返回
    ----
    dict：``等级`` / ``依据`` / ``规则ID`` / ``规则版本`` / ``公式`` /
    ``状态``（离散化后的三层状态）/ ``输入``（原值与可用性）。
    """
    states = {
        "Q": _q_state(Q),
        "D": _d_state(D, reliable=d_reliable),
        "P": _p_state(P, reliable=p_reliable),
    }
    rule = _match(states)
    return {
        "等级": rule["level"],
        "依据": rule["reason"](states),
        "规则ID": rule["id"],
        "规则版本": RULE_VERSION,
        "公式": RULE_TEXT,
        "状态": dict(states),
        "输入": {
            "Q": Q, "D": D, "P": P,
            "D可信": bool(d_reliable), "P可信": bool(p_reliable),
            "Q可用": states["Q"] != Q_MISSING,
            "D可用": states["D"] != D_MISSING,
            "P可用": states["P"] != P_MISSING,
        },
    }


def describe_rules():
    """导出决策表（供文档生成与测试自检使用）。"""
    rows = []
    for r in RULES:
        rows.append({
            "规则ID": r["id"],
            "Q状态": list(r["q"]),
            "D状态": list(r["d"]),
            "P状态": list(r["p"]),
            "等级": r["level"],
            "组合数": len(r["q"]) * len(r["d"]) * len(r["p"]),
        })
    return {"规则版本": RULE_VERSION, "公式": RULE_TEXT, "规则表": rows}
