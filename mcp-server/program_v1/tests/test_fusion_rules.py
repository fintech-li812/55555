# -*- coding: utf-8 -*-
"""融合规则 FUSE(Q, D, P) 消歧后的验证用例。

覆盖 docs/融合规则口径冲突消歧报告_20261028.md 中列出的 15 处冲突，
按用户要求的五类场景组织：
  1. 冲突条目定位（C-1~C-3、C-13~C-14）—— 用「修改前后对比」的固定用例锁死；
  2. 冲突导致的失效/不确定/互相抵消（C-4~C-6）—— 完备性、互斥性、顺序无关性；
  3. 统一术语后的规则表述（决策表结构与文案）；
  4. 空值/缺失/越界/降级/多源冲突/顺序变化；
  5. 修改前后对比 + 与 v2 legacy 口径的交叉校验。

设计原则（与 test_v8_gap_closure.py 一致）：
  * 不依赖数据库，可离线运行；
  * 每条断言写明「为什么这是期望值」；
  * 不 skip、不 xfail，失败即失败。

运行：
    python mcp-server/program_v1/tests/test_fusion_rules.py
    python -m pytest mcp-server/program_v1/tests/test_fusion_rules.py -v
"""
from __future__ import annotations

import ast
import itertools
import math
import os
import random
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from v8 import fusion as F                       # noqa: E402
from v8 import indicators as ind                 # noqa: E402
from v8 import layers as L                       # noqa: E402

# ---------------------------------------------------------------------------
# 状态 → 代表值 映射（用于遍历 48 种组合）
# 每个代表值必须落在该状态的区间内部，不能取在边界上
# ——边界值由专门的边界用例覆盖。
REPR = {
    "Q": {F.Q_MISSING: None, F.Q_BAD: 0.1, F.Q_WARN: 0.4, F.Q_OK: 0.8},
    "D": {F.D_MISSING: None, F.D_RISK: -0.1, F.D_OK: 0.1},
    "P": {F.P_MISSING: None, F.P_HIGH: 0.9, F.P_MID: 0.5, F.P_LOW: 0.1},
}

COMBOS = list(itertools.product(F.ALL_Q, F.ALL_D, F.ALL_P))


def _call(qs, ds, ps):
    """按状态三元组调用 fuse；D/P 的「缺失」通过显式可靠性标志传递。"""
    return F.fuse(REPR["Q"][qs], REPR["D"][ds], REPR["P"][ps],
                  d_reliable=(ds != F.D_MISSING),
                  p_reliable=(ps != F.P_MISSING))


# ---------------------------------------------------------------------------
# 1. 决策表的结构性质：完备 / 互斥 / 顺序无关
# ---------------------------------------------------------------------------


class TestDecisionTableStructure(unittest.TestCase):
    """第 2 类要求：证明冲突消除后规则在任意输入下都稳定生效。"""

    def test_completeness_all_48_combinations_resolve(self):
        """48 种状态组合（4×3×4）必须全部得到明确等级，不得抛异常。"""
        self.assertEqual(len(COMBOS), 48, "状态组合数应为 4×3×4=48")
        for qs, ds, ps in COMBOS:
            with self.subTest(Q=qs, D=ds, P=ps):
                r = _call(qs, ds, ps)
                self.assertIn(r["等级"], ("红灯", "黄灯", "绿灯"))
                self.assertTrue(r["依据"], "依据不得为空，否则留痕不可追溯")
                self.assertTrue(r["规则ID"].startswith("FUSE-R"))

    def test_mutual_exclusivity_each_combo_matches_exactly_one_rule(self):
        """互斥性：任一状态组合必须**恰好**命中一条规则。

        原实现用「或」连接条件，红灯与黄灯的条件集合存在交集，
        因此只有靠 if/elif 的书写顺序才能出结果（C-5）。
        新规则若出现 0 条或 ≥2 条命中，说明又退化成了顺序依赖。
        """
        for qs, ds, ps in COMBOS:
            hits = [r["id"] for r in F.RULES
                    if qs in r["q"] and ds in r["d"] and ps in r["p"]]
            with self.subTest(Q=qs, D=ds, P=ps):
                self.assertEqual(len(hits), 1,
                                 "状态组合 %s/%s/%s 命中 %d 条规则：%s"
                                 % (qs, ds, ps, len(hits), hits))

    def test_coverage_counts_sum_to_48(self):
        """各规则覆盖的组合数之和必须等于 48 —— 不多（重叠）不少（空洞）。"""
        total = sum(len(r["q"]) * len(r["d"]) * len(r["p"]) for r in F.RULES)
        self.assertEqual(total, 48,
                         "决策表覆盖 %d 种组合，应为 48（说明有重叠或空洞）" % total)

    def test_order_independence(self):
        """顺序无关性：随机打乱规则书写顺序，48 种组合的判定结果必须不变。

        这是 C-5（结果依赖 if/elif 顺序）的直接回归测试。
        旧实现把规则顺序换一下，Q<0.3 且 D<0 且 0.3≤P<0.7 就会
        从红灯变成黄灯；新实现因规则互斥，顺序改变不应有任何影响。
        """
        baseline = {(qs, ds, ps): _call(qs, ds, ps)["规则ID"]
                    for qs, ds, ps in COMBOS}

        original = list(F.RULES)
        try:
            for seed in (1, 7, 42, 20261028, 99991):
                shuffled = list(original)
                random.Random(seed).shuffle(shuffled)
                F.RULES[:] = shuffled
                for qs, ds, ps in COMBOS:
                    with self.subTest(seed=seed, Q=qs, D=ds, P=ps):
                        self.assertEqual(_call(qs, ds, ps)["规则ID"],
                                         baseline[(qs, ds, ps)],
                                         "打乱规则顺序后判定结果发生了变化")
        finally:
            F.RULES[:] = original

        # 复原后必须与基线一致，排除测试自身污染
        self.assertEqual({(qs, ds, ps): _call(qs, ds, ps)["规则ID"]
                          for qs, ds, ps in COMBOS}, baseline)

    def test_no_unreachable_rule(self):
        """决策表里不得存在永远命中不到的规则（死规则）。"""
        used = {_call(qs, ds, ps)["规则ID"] for qs, ds, ps in COMBOS}
        declared = {r["id"] for r in F.RULES}
        self.assertEqual(used, declared,
                         "存在未被任何状态组合命中的规则：%s"
                         % (declared - used))


# ---------------------------------------------------------------------------
# 2. 空值 / 缺失 / 非数值 / 越界（第 4 类要求）
# ---------------------------------------------------------------------------


class TestInputValidity(unittest.TestCase):
    """原实现对这些输入要么抛 TypeError 崩溃，要么静默给出结论（C-7、C-15）。"""

    BAD_VALUES = [None, float("nan"), float("inf"), float("-inf"),
                  "0.8", "", [], {}, True, False]

    def test_invalid_Q_never_crashes_and_is_reported(self):
        """Q 不可用 → 红灯，并在返回里标注 Q 不可用（C-7：原实现 TypeError）。"""
        for bad in self.BAD_VALUES:
            with self.subTest(Q=repr(bad)):
                r = F.fuse(bad, 0.1, 0.1)
                self.assertEqual(r["等级"], "红灯")
                self.assertEqual(r["规则ID"], "FUSE-R1")
                self.assertFalse(r["输入"]["Q可用"])
                self.assertIn("不可用", r["依据"])

    def test_invalid_D_never_crashes_and_is_reported(self):
        """D 不可用 → 黄灯（第二层缺失，无法判定健康，但不等于有风险）。"""
        for bad in self.BAD_VALUES:
            with self.subTest(D=repr(bad)):
                r = F.fuse(0.8, bad, 0.1)
                self.assertEqual(r["等级"], "黄灯")
                self.assertEqual(r["规则ID"], "FUSE-R3")
                self.assertFalse(r["输入"]["D可用"])

    def test_invalid_P_blocks_green_light(self):
        """P 不可用 → 证据链不完整，不允许放行绿灯（原实现会崩或误判）。"""
        for bad in self.BAD_VALUES:
            with self.subTest(P=repr(bad)):
                r = F.fuse(0.8, 0.1, bad)
                self.assertEqual(r["等级"], "黄灯")
                self.assertEqual(r["规则ID"], "FUSE-R9")
                self.assertFalse(r["输入"]["P可用"])

    def test_out_of_range_probability_is_rejected_not_clamped(self):
        """Q/P 越界（<0 或 >1）判为不可用，而不是裁剪到边界后继续用。

        裁剪会伪造出一个「看起来合法」的输入，属于静默降级，故明确拒绝。
        """
        for bad in (-0.001, 1.001, 2.0, -5.0):
            with self.subTest(Q=bad):
                self.assertEqual(F.fuse(bad, 0.1, 0.1)["等级"], "红灯")
            with self.subTest(P=bad):
                self.assertEqual(F.fuse(0.8, 0.1, bad)["等级"], "黄灯")

    def test_boundary_values_are_accepted(self):
        """合法边界 0.0 与 1.0 必须被接受（容差 1e-9 内），不得误判为不可用。"""
        r = F.fuse(0.0, 0.1, 0.0)
        self.assertTrue(r["输入"]["Q可用"])
        self.assertTrue(r["输入"]["P可用"])
        r = F.fuse(1.0, 0.1, 1.0)
        self.assertEqual(r["规则ID"], "FUSE-R6")   # Q=1 达标、D≥0、P=1 高

    def test_float_noise_at_upper_bound_is_tolerated(self):
        """Q = 1.0000000004 这类浮点噪声应视为合法（容差 1e-9）。"""
        r = F.fuse(1.0 + 4e-10, 0.1, 0.1)
        self.assertTrue(r["输入"]["Q可用"], "边界浮点噪声应被容差吸收")
        self.assertEqual(r["等级"], "绿灯")

    def test_integer_inputs_are_accepted(self):
        """int 是合法输入（0/1 常见于二值化的质量与概率）。"""
        self.assertEqual(F.fuse(1, 0, 0)["等级"], "绿灯")
        self.assertEqual(F.fuse(0, 0, 0)["等级"], "红灯")


# ---------------------------------------------------------------------------
# 3. 上游降级：DEA-DA 求解失败 / 随机森林未训练出树
# ---------------------------------------------------------------------------


class TestUpstreamDegradation(unittest.TestCase):
    """C-8、C-9：上游不可信时，不能把「无法判断」当成「无风险」。"""

    def test_degraded_deada_does_not_produce_green(self):
        """D 求解降级 → 不得放行绿灯，即使 Q 与 P 都很好。"""
        r = F.fuse(0.9, 0.5, 0.05, d_reliable=False)
        self.assertEqual(r["等级"], "黄灯")
        self.assertEqual(r["规则ID"], "FUSE-R3")
        self.assertFalse(r["输入"]["D可信"])
        self.assertIn("降级", r["依据"])

    def test_zero_trees_does_not_mean_no_fraud(self):
        """树数=0 时 P 恒为 0.0，若照旧解读成「P<0.3 排除粉饰」会严重误判。

        benford_rf 在所有 bootstrap 样本标签单一时返回 P=0.0，
        这是「模型没训练出来」，不是「没有舞弊」。
        """
        r = F.fuse(0.9, 0.5, 0.0, p_reliable=False)
        self.assertEqual(r["等级"], "黄灯")
        self.assertEqual(r["规则ID"], "FUSE-R9")
        self.assertFalse(r["输入"]["P可信"])
        # 对照组：同样的 P=0.0，若第三层确实可用，则是真绿灯
        self.assertEqual(F.fuse(0.9, 0.5, 0.0, p_reliable=True)["等级"], "绿灯")

    def test_legacy_three_arg_wrapper_ignores_degradation(self):
        """记录型用例：layers.fuse 的兼容包装不感知降级，这是它必须被弃用的原因。"""
        good = F.fuse(0.9, 0.5, 0.05, d_reliable=False)
        legacy = L.fuse(0.9, 0.5, 0.05)
        self.assertEqual(good["等级"], "黄灯")
        self.assertEqual(legacy["等级"], "绿灯",
                         "兼容包装未传 d_reliable，会漏掉降级；新代码应直接调用 fusion.fuse")


# ---------------------------------------------------------------------------
# 4. 阈值边界（C-1、C-3：符号与区间统一为「≥ 下界」）
# ---------------------------------------------------------------------------


class TestThresholdBoundaries(unittest.TestCase):
    """边界取值的归属必须唯一且可预期。"""

    def test_P_exactly_0_7_is_high(self):
        """C-1：P=0.7 归入「高」（采用 ≥，与 2-A.7/4.0/原代码一致）。

        2-A.3 写的是 P > 0.7，与之矛盾；本用例即该笔误的裁决记录。
        P 由 benford_rf 四舍五入到 4 位，0.7000 完全可能出现（如 42/60 票）。
        """
        self.assertEqual(F._p_state(0.7), F.P_HIGH)
        # D<0 时 → 红灯
        self.assertEqual(F.fuse(0.8, -0.1, 0.7)["等级"], "红灯")
        # D≥0 时 → 黄灯（结论冲突，不放行绿灯）
        self.assertEqual(F.fuse(0.8, 0.1, 0.7)["等级"], "黄灯")

    def test_P_exactly_0_3_is_mid_not_low(self):
        """P=0.3 归入「中」，不享有 P<0.3 的降级语义。"""
        self.assertEqual(F._p_state(0.3), F.P_MID)
        r = F.fuse(0.8, -0.1, 0.3)
        self.assertEqual(r["等级"], "黄灯")
        self.assertIn("灰区", r["依据"])

    def test_Q_exactly_0_3_and_0_5(self):
        """C-3：Q=0.3 属「存疑」（不再是「严重不足」），Q=0.5 属「达标」。"""
        self.assertEqual(F._q_state(0.3), F.Q_WARN)
        self.assertEqual(F._q_state(0.5), F.Q_OK)
        self.assertEqual(F._q_state(0.2999), F.Q_BAD)
        # Q=0.3 且 D≥0 且 P 低 → 未达绿灯门槛 → 黄灯（原实现靠兜底，无明文规则）
        r = F.fuse(0.3, 0.1, 0.1)
        self.assertEqual(r["等级"], "黄灯")
        self.assertEqual(r["规则ID"], "FUSE-R7")
        # Q=0.5 达标 → 绿灯
        self.assertEqual(F.fuse(0.5, 0.1, 0.1)["等级"], "绿灯")

    def test_D_exactly_zero_is_healthy(self):
        """D=0 属「正常」（口径是 D<0 判风险，0 不属风险）。"""
        self.assertEqual(F._d_state(0.0), F.D_OK)
        self.assertEqual(F._d_state(-0.0), F.D_OK)
        self.assertEqual(F._d_state(-1e-12), F.D_RISK)


# ---------------------------------------------------------------------------
# 5. 修改前后对比（第 5 类要求）
# ---------------------------------------------------------------------------


class TestBeforeAfterComparison(unittest.TestCase):
    """把「旧规则会给出什么」与「新规则给什么」逐条固定下来。"""

    def test_C2_conflict_cell_P_high_D_healthy(self):
        """C-2 的冲突格：D≥0 且 P≥0.7。

        旧规则：完全忽略 P，判绿灯。
        新规则：判黄灯（FUSE-R6），依据为「两个结论冲突，不放行绿灯」。

        这是本次消歧唯一的行为变化，也是唯一需要业务确认的一格。
        """
        r = F.fuse(0.5667, 0.187676, 0.9833)
        self.assertEqual(r["等级"], "黄灯")
        self.assertEqual(r["规则ID"], "FUSE-R6")
        self.assertIn("冲突", r["依据"])

    def test_red_light_conditions_unchanged(self):
        """原红灯口径保持不变：Q<0.3，或 (D<0 且 P≥0.7)。"""
        self.assertEqual(F.fuse(0.2, 0.5, 0.0)["等级"], "红灯")    # Q<0.3
        self.assertEqual(F.fuse(0.8, -0.1, 0.9)["等级"], "红灯")   # D<0 且 P≥0.7
        self.assertEqual(F.fuse(0.5667, -0.1, 0.9833)["等级"], "红灯")

    def test_green_light_conditions_tightened_only_by_P(self):
        """绿灯条件仅在 P 上收紧：由「D≥0 且 Q≥0.5」变为「且 P<0.7 且 P 可用」。"""
        # 新旧一致的绿灯
        self.assertEqual(F.fuse(0.8, 0.1, 0.1)["等级"], "绿灯")
        self.assertEqual(F.fuse(0.5, 0.0, 0.6999)["等级"], "绿灯")
        # 新增的两类拦截
        self.assertEqual(F.fuse(0.8, 0.1, 0.7)["等级"], "黄灯")    # P 达高
        self.assertEqual(F.fuse(0.8, 0.1, None)["等级"], "黄灯")   # P 不可用

    def test_C4_grey_zone_now_has_explicit_rule(self):
        """C-4：Q∈[0.3,0.5) 且 D≥0 原靠「未完全满足绿灯条件」兜底，现为明文 R7/R8。"""
        for q in (0.3, 0.35, 0.4999):
            with self.subTest(Q=q):
                r = F.fuse(q, 0.1, 0.1)
                self.assertEqual(r["规则ID"], "FUSE-R7")
                self.assertIn("未达绿灯门槛", r["依据"])
        # 同区间但 P 不可用 → R8（依据不同，需可区分）
        self.assertEqual(F.fuse(0.4, 0.1, None)["规则ID"], "FUSE-R8")

    def test_C10_reason_distinguishes_downgrade_from_grey_zone(self):
        """C-10：同为黄灯，P<0.3（降级）与 0.3≤P<0.7（灰区）的依据必须可区分。"""
        low = F.fuse(0.8, -0.1, 0.1)
        mid = F.fuse(0.8, -0.1, 0.5)
        miss = F.fuse(0.8, -0.1, None, p_reliable=False)
        self.assertEqual(low["等级"], mid["等级"])
        self.assertIn("降级", low["依据"])
        self.assertIn("灰区", mid["依据"])
        self.assertIn("无法排除粉饰", miss["依据"])
        self.assertEqual(len({low["依据"], mid["依据"], miss["依据"]}), 3,
                         "三种情形的依据必须互不相同，否则留痕无法追溯")

    def test_C5_order_dependence_is_gone(self):
        """C-5：旧实现的重叠格 Q<0.3 且 D<0 且 0.3≤P<0.7。

        旧规则：红灯条件 (Q<0.3) 与黄灯条件 (Q<0.5 且 D<0) 同时成立，
               靠 if/elif 顺序判红灯；若调换顺序则变黄灯。
        新规则：Q<0.3 属 R2，黄灯的 Q 状态只含「存疑/达标」，两者不相交，
               判定唯一且为红灯。
        """
        r = F.fuse(0.2, -0.1, 0.5)
        self.assertEqual(r["等级"], "红灯")
        self.assertEqual(r["规则ID"], "FUSE-R2")
        # 黄灯规则确实不再包含「严重不足」状态
        yellow_q_states = set()
        for rule in F.RULES:
            if rule["level"] == "黄灯":
                yellow_q_states |= set(rule["q"])
        self.assertNotIn(F.Q_BAD, yellow_q_states)


# ---------------------------------------------------------------------------
# 6. 多源冲突：与 v2 legacy_fuse 的交叉校验
# ---------------------------------------------------------------------------


class TestLegacyCrossCheck(unittest.TestCase):
    """C-14：同一业务存在两套规则（v2 legacy_fuse 与 v8 fusion.fuse）。

    legacy 明确标注为「legacy screening; not fraud evidence」，
    保留用于向后对比，因此**不删除**，但用测试把两套规则的差异面固定住，
    防止它们各自演化、静默漂移。
    """

    @staticmethod
    def _legacy():
        """取出 legacy_fuse 的真实源码执行，而不是 import 整个模块。

        risk_model.py 在模块级 import numpy/scipy，仅为这两行融合规则
        去装科学计算栈并不划算；但直接把规则抄进测试就失去了防漂移的意义。
        因此这里用 ast 从源文件里裁出 ``number`` 与 ``legacy_fuse`` 两个
        纯 Python 函数，在其真实源码上做校验——源码一改，本用例照样会失败。
        """
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        path = os.path.join(root, "model", "现金流风控_v2", "risk_model.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        ns = {"math": math}
        for node in ast.parse(src).body:
            if isinstance(node, ast.FunctionDef) and node.name in ("number", "legacy_fuse"):
                exec(compile(ast.get_source_segment(src, node), path, "exec"), ns)
        if "legacy_fuse" not in ns:
            raise AssertionError("未能从 risk_model.py 提取到 legacy_fuse，路径或函数名已变更")
        return ns["legacy_fuse"]

    def test_two_rule_sets_agree_except_one_documented_cell(self):
        """在合法输入网格上，两套规则应只有一格不同。

        预期差异：q≥0.5 且 d≥0 且 vote≥0.7
          legacy → 绿灯（第三层输出被完全忽略）
          v8     → 黄灯（FUSE-R6：结论冲突，不放行绿灯）
        """
        legacy_fuse = self._legacy()
        diffs = []
        for q in (0.1, 0.4, 0.8):
            for d in (-0.1, 0.1):
                for vote in (0.1, 0.5, 0.9):
                    old = legacy_fuse(q, d, vote)["light"]
                    new = F.fuse(q, d, vote)["等级"]
                    if old != new:
                        diffs.append((q, d, vote, old, new))
        self.assertEqual(diffs, [(0.8, 0.1, 0.9, "绿灯", "黄灯")],
                         "与 legacy 的差异面发生了变化，需重新确认口径裁决：%s" % diffs)

    def test_legacy_rejects_invalid_while_v8_reports(self):
        """同样的非法输入：legacy 抛 ValueError，v8 返回明确等级 + 不可用标注。

        两者都「不静默通过」，只是处理策略不同：legacy 面向交互式调用，
        v8 面向定时任务，抛异常会让整批评估中断。
        """
        legacy_fuse = self._legacy()
        with self.assertRaises(ValueError):
            legacy_fuse(1.5, 0.1, 0.1)
        r = F.fuse(1.5, 0.1, 0.1)
        self.assertEqual(r["等级"], "红灯")
        self.assertFalse(r["输入"]["Q可用"])


# ---------------------------------------------------------------------------
# 7. 端到端：真实三层输出喂给融合规则
# ---------------------------------------------------------------------------


class TestEndToEnd(unittest.TestCase):
    """用真实的三层模型输出验证融合规则，而非只测合成数字。"""

    def test_three_scenarios_produce_traceable_results(self):
        for sc in ("healthy", "edge", "risk"):
            with self.subTest(scenario=sc):
                pool = ind.build_sample_pool(20)
                values = ind.TARGET_SCENARIOS[sc]["values"]
                gate = L.quality_gate(pool)
                da = L.deada(pool, values)
                rf = L.benford_rf(pool, values, gate)
                r = F.fuse(gate["Q"], da["D"], rf["P"],
                           d_reliable=not da.get("降级", False),
                           p_reliable=rf.get("树数", 0) > 0)
                self.assertIn(r["等级"], ("红灯", "黄灯", "绿灯"))
                self.assertTrue(r["规则ID"])
                # 三条输入必须都落在合法区间，否则说明上游算错了
                for k in ("Q", "D", "P"):
                    self.assertTrue(r["输入"]["%s可用" % k],
                                    "场景 %s 的 %s 不可用：%s" % (sc, k, r["输入"][k]))

    def test_independent_layers_match_full_assessment(self):
        """独立调用三层后融合，与整包评估路径的结果必须一致。"""
        sys.path.insert(0, os.path.join(SRC))
        from v8 import engine
        a = engine.assess(industry="制造业", scenario="edge",
                          amount=120000.0, n_per_group=20)
        pool = ind.build_sample_pool(20)
        values = ind.TARGET_SCENARIOS["edge"]["values"]
        gate = L.quality_gate(pool)
        da = L.deada(pool, values)
        rf = L.benford_rf(pool, values, gate)
        direct = F.fuse(gate["Q"], da["D"], rf["P"],
                        d_reliable=not da.get("降级", False),
                        p_reliable=rf.get("树数", 0) > 0)
        self.assertEqual(a["融合判定"]["等级"], direct["等级"])
        self.assertEqual(a["融合判定"]["规则ID"], direct["规则ID"])

    def test_final_level_never_lower_than_fused(self):
        """取严逻辑：最终等级不得低于三层融合等级（这是「取较严格者」的定义）。"""
        rank = {"绿灯": 0, "黄灯": 1, "红灯": 2}
        for sc in ("healthy", "edge", "risk"):
            with self.subTest(scenario=sc):
                from v8 import engine
                a = engine.assess(scenario=sc)
                self.assertGreaterEqual(
                    rank[a["最终等级"]["等级"]], rank[a["融合判定"]["等级"]],
                    "最终等级比融合等级更宽松，取严逻辑失效")

    def test_missing_buffer_dimension_is_disclosed(self):
        """C-12：缓冲维度缺失时，口径文案必须说明，不能仍宣称「两层并行取严」。"""
        from v8 import engine
        a = engine.assess(scenario="edge")
        final = a["最终等级"]
        if not final["缓冲维度参与比较"]:
            self.assertIn("缓冲维度未参与比较", final["口径"])
            self.assertNotIn("现金流缓冲判定（相对阈值 M×K）",
                             final["口径"].split("（参与维度：")[0])
        else:
            # 两个维度都参与：文案必须如数列举，不能笼统写「两层并行」
            note = final["口径"]
            self.assertIn("参与维度", note)
            self.assertIn("三层融合判定 FUSE(Q,D,P)", note)
            self.assertIn("现金流缓冲判定（相对阈值 M×K）", note)


# ---------------------------------------------------------------------------
# 8. 规则表可导出（供文档与审计核对）
# ---------------------------------------------------------------------------


class TestRuleDocumentation(unittest.TestCase):

    def test_describe_rules_exportable(self):
        d = F.describe_rules()
        self.assertEqual(d["规则版本"], F.RULE_VERSION)
        self.assertEqual(len(d["规则表"]), 10)
        self.assertEqual(sum(r["组合数"] for r in d["规则表"]), 48)
        self.assertTrue(d["公式"])

    def test_rule_text_matches_implementation(self):
        """文档里的公式文本必须与实际判定一致（抽样验证，防止文案漂移）。"""
        self.assertIn("P≥0.7", F.RULE_TEXT)
        self.assertIn("Q<0.3", F.RULE_TEXT)
        # 绿灯条件与实现一致
        self.assertEqual(F.fuse(0.5, 0.0, 0.6999)["等级"], "绿灯")
        self.assertNotEqual(F.fuse(0.5, 0.0, 0.7)["等级"], "绿灯")


if __name__ == "__main__":
    unittest.main(verbosity=2)
