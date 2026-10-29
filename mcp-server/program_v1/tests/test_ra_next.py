# -*- coding: utf-8 -*-
"""RA_NEXT（ra-next-0.1）工程适配层回归用例。

分四组：
  1. 黄金值——与 Collex 候选 `algo/RA_NEXT/results.json` 逐位一致，
     防止适配层"顺手优化"把算法改掉；
  2. 可用性语义——不可用必须是 None + reason，不得变成 0.0 被读成"无风险"；
  3. 确定性——同一输入重复调用结果完全相同；
  4. 双版本并存——基线 v8 与 ra-next-0.1 共用同一张融合表，灯色不因换版漂移。

运行：
    python mcp-server/program_v1/tests/test_ra_next.py
"""
from __future__ import annotations

import os
import sys
import unittest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from v8 import engine, fusion, indicators as ind, ra_next  # noqa: E402

# 黄金值来源：algo/RA_NEXT/results.json（ra-next-0.1，种子 20261029/20261027）
GOLDEN_Q = 0.9333
GOLDEN_D = {"healthy": 0.868543, "edge": 0.187676, "risk": -0.471113}
GOLDEN_P = {"healthy": 0.0297, "edge": 0.8312, "risk": 0.9666}
GOLDEN_LIGHT = {"healthy": "绿灯", "edge": "黄灯", "risk": "红灯"}


class TestGoldenValues(unittest.TestCase):
    """1. 与候选逐位一致。"""

    def test_q(self):
        gate = ra_next.quality_gate(ind.build_sample_pool())
        self.assertTrue(gate["available"])
        self.assertEqual(gate["Q"], GOLDEN_Q)
        self.assertEqual(gate["检验数"], 30)
        self.assertEqual(gate["校正"], "Holm")

    def test_d(self):
        pool = ind.build_sample_pool()
        for key, want in GOLDEN_D.items():
            with self.subTest(scenario=key):
                got = ra_next.deada(pool, ind.TARGET_SCENARIOS[key]["values"])
                self.assertTrue(got["available"])
                self.assertAlmostEqual(got["D"], want, places=9)

    def test_p(self):
        pool = ind.build_sample_pool()
        for key, want in GOLDEN_P.items():
            with self.subTest(scenario=key):
                got = ra_next.benford_rf(pool, ind.TARGET_SCENARIOS[key]["values"])
                self.assertTrue(got["available"])
                self.assertEqual(got["P"], want)
                self.assertEqual(got["树数"], 60)
                self.assertIsNotNone(got["OOB_Brier"])

    def test_lights_unchanged(self):
        pool = ind.build_sample_pool()
        gate = ra_next.quality_gate(pool)
        for key, want in GOLDEN_LIGHT.items():
            with self.subTest(scenario=key):
                target = ind.TARGET_SCENARIOS[key]["values"]
                da, rf = ra_next.deada(pool, target), ra_next.benford_rf(pool, target)
                got = fusion.fuse(gate["Q"], da["D"], rf["P"])
                self.assertEqual(got["等级"], want)


class TestAvailability(unittest.TestCase):
    """2. 不可用必须显式表达，不得伪装成 0。"""

    def test_empty_pool_q_unavailable(self):
        gate = ra_next.quality_gate([])
        self.assertIsNone(gate["Q"])
        self.assertFalse(gate["available"])
        self.assertIn("空", gate["reason"])

    def test_non_finite_input_unavailable(self):
        pool = [{"code": "X", "label": 0, "values": {k: float("nan") for k in ind.KEYS}}]
        gate = ra_next.quality_gate(pool)
        self.assertIsNone(gate["Q"])
        self.assertFalse(gate["available"])

    def test_single_class_deada_unavailable(self):
        pool = [{"code": "A", "label": 1, "values": ind.TARGET_SCENARIOS["risk"]["values"]}] * 10
        got = ra_next.deada(pool, ind.TARGET_SCENARIOS["edge"]["values"])
        self.assertIsNone(got["D"])
        self.assertTrue(got["降级"])
        self.assertFalse(got["available"])

    def test_zero_trees_p_unavailable(self):
        pool = ind.build_sample_pool()
        got = ra_next.benford_rf(pool, ind.TARGET_SCENARIOS["edge"]["values"], n_trees=0)
        self.assertIsNone(got["P"])
        self.assertEqual(got["树数"], 0)
        self.assertFalse(got["available"])

    def test_missing_target_key_unavailable(self):
        pool = ind.build_sample_pool()
        for fn in (lambda t: ra_next.deada(pool, t),
                   lambda t: ra_next.benford_rf(pool, t)):
            got = fn({"X1": 0.1})
            self.assertIn(got.get("D", got.get("P")), (None,))

    def test_unavailable_p_never_becomes_green(self):
        """P 不可用 + Q/D 都好 → 必须黄灯（证据链不完整），不能绿灯。"""
        got = fusion.fuse(0.9, 0.5, None, p_reliable=False)
        self.assertEqual(got["等级"], "黄灯")
        self.assertEqual(got["规则ID"], "FUSE-R9")


class TestDeterminism(unittest.TestCase):
    """3. 同一输入重复调用逐位相同。"""

    def test_repeat_identical(self):
        pool = ind.build_sample_pool()
        target = ind.TARGET_SCENARIOS["edge"]["values"]
        first = ra_next.benford_rf(pool, target)
        for _ in range(3):
            self.assertEqual(first, ra_next.benford_rf(pool, target))

    def test_no_wallclock_fields(self):
        """返回值里不得出现耗时等非确定性字段，否则无法做逐位比对。"""
        got = ra_next.benford_rf(ind.build_sample_pool(), ind.TARGET_SCENARIOS["edge"]["values"])
        self.assertFalse([k for k in got if "耗时" in k or "time" in k.lower()])


class TestEngineDualVersion(unittest.TestCase):
    """4. 双版本并存：同一融合表，D 与灯色不漂移。"""

    @classmethod
    def setUpClass(cls):
        cls.base = engine.assess(scenario="edge", algo="v8")
        cls.next = engine.assess(scenario="edge", algo=ra_next.VERSION)

    def test_algorithm_labels(self):
        self.assertEqual(self.base["算法版本"], "v8-baseline")
        self.assertEqual(self.next["算法版本"], ra_next.VERSION)

    def test_d_identical_across_versions(self):
        """D 的口径未变，换版本不应改变判别得分。"""
        self.assertAlmostEqual(self.next["第二层"]["D"], self.base["第二层"]["D"], places=9)

    def test_final_level_identical(self):
        self.assertEqual(self.next["最终等级"]["等级"], self.base["最终等级"]["等级"])

    def test_q_scale_warning_present(self):
        """新版 Q 全过时，必须带阈值适用性提示，不静默放行。"""
        self.assertIn("阈值适用性", self.next["第一层"])

    def test_unknown_algo_rejected(self):
        with self.assertRaises(ValueError):
            engine.assess(algo="ra-v9.9")

    def test_stability_opt_in(self):
        got = engine.assess(scenario="edge", algo=ra_next.VERSION, with_stability=True)
        self.assertIsNotNone(got["稳定性"])
        self.assertIsNotNone(got["稳定性"]["D_区间"])
        self.assertIsNone(engine.assess(scenario="edge", algo="v8")["稳定性"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
