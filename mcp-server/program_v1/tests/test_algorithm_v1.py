# -*- coding: utf-8 -*-
"""生产算法包 v8.algorithm 的回归测试。

覆盖四类：
1. **锚点**：三场景 Q / D / P 与历史版本逐位一致（换实现不许漂移）；
2. **证书**：LP 对偶最优性、已知最优值、不可行 / 无界 / 冗余行的显式失败；
3. **契约**：参数错误抛异常、数据问题返回不可用、绝不静默冒充成功；
4. **政策**：证据只升不降、动态阈值、统一视图。

可单独运行：python mcp-server/program_v1/tests/test_algorithm_v1.py
"""
from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
import time
import unittest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from v8 import indicators as ind  # noqa: E402
from v8 import algorithm as alg  # noqa: E402
from v8.algorithm import (calibration, deada, forest, policy, quality,  # noqa: E402
                          simplex, validation)

POOL = ind.build_sample_pool(20)


def scenario(key: str) -> dict:
    return dict(ind.TARGET_SCENARIOS[key]["values"])


class TestAnchors(unittest.TestCase):
    """历史锚点：换实现不许漂移。"""

    def test_anchors(self):
        for sc, d_expect, p_expect in (("healthy", 0.868543, 0.0297),
                                       ("edge", 0.187676, 0.8312),
                                       ("risk", -0.471113, 0.9666)):
            r = alg.assess(POOL, scenario(sc))
            self.assertEqual(r["第一层"]["Q"], 0.9333, sc)
            self.assertEqual(r["第二层"]["D"], d_expect, sc)
            self.assertEqual(r["第三层"]["P"], p_expect, sc)

    def test_same_as_ra_next_01(self):
        """与已接入的 ra-next-0.1 逐位一致，保证切换不产生意外跳变。"""
        from v8 import ra_next
        for sc in ("healthy", "edge", "risk"):
            old = ra_next.benford_rf(POOL, scenario(sc), None)
            new = alg.assess(POOL, scenario(sc))["第三层"]
            self.assertEqual(old["P"], new["P"], sc)
            # v1 的 P_raw 保留 6 位（比旧版 4 位更精细），只比到旧版精度
            self.assertAlmostEqual(old["P_raw"], new["P_raw"], places=4, msg=sc)
            self.assertEqual(old["树数"], new["树数"], sc)
        self.assertEqual(ra_next.quality_gate(POOL)["Q"],
                         alg.assess(POOL, scenario("edge"))["第一层"]["Q"])
        self.assertEqual(ra_next.deada(POOL, scenario("edge"))["D"],
                         alg.assess(POOL, scenario("edge"))["第二层"]["D"])

    def test_fusion_table_unchanged(self):
        """融合表仍是 fuse-v2.0，本包不复制、不改写。"""
        from v8 import fusion
        r = alg.assess(POOL, scenario("risk"))
        self.assertEqual(r["融合判定"]["规则ID"], "FUSE-R4")
        self.assertEqual(fusion.RULE_VERSION, "fuse-v2.0")


class TestSimplex(unittest.TestCase):
    """LP 求解器与对偶证书。"""

    def test_known_optimum(self):
        x, obj, ok, diag = simplex.solve_verified([3, 2, 0, 0],
                                                  [[1, 1, 1, 0], [1, 0, 0, 1]], [4, 2])
        self.assertTrue(ok)
        self.assertTrue(diag["solved"])
        self.assertAlmostEqual(obj, 10.0)
        self.assertTrue(diag["optimality_verified"])

    def test_negative_rhs(self):
        _, _, _, diag = simplex.solve_verified([1], [[-1]], [-2])
        self.assertTrue(diag["optimality_verified"])

    def test_redundant_row(self):
        """冗余约束：基线路会误判为无解，备选路径必须接管并标明来源。

        这是接手后新发现并修掉的一处缺陷：题目 max x, s.t. x=1, 2x=2
        显然有解 x=1，但基线 _simplex 因人工变量留在基里而整题判失败。
        """
        x, obj, ok, diag = simplex.solve_verified([1], [[1], [2]], [1, 2])
        self.assertTrue(ok)
        self.assertTrue(diag["optimality_verified"])
        self.assertIn("fallback", diag["solver"])
        self.assertEqual(round(obj, 9), 1.0)
        self.assertEqual(round(x[0], 9), 1.0)

    def test_infeasible(self):
        _, _, ok, diag = simplex.solve_verified([1], [[1], [1]], [1, 2])
        self.assertFalse(ok)
        self.assertIn("不可行", diag["reason"])

    def test_unbounded(self):
        _, _, ok, _ = simplex.solve_verified([1, 0], [[0, 1]], [1])
        self.assertFalse(ok)

    def test_certificate_rejects_nonoptimal(self):
        # 可行但非最优：对偶间隙必然暴露
        cert = simplex.certificate([1, 0], [[1, 1]], [10], [0, 10], [1])
        self.assertFalse(cert["optimality_verified"])
        # 可行但对偶不可行
        cert = simplex.certificate([1, 0], [[1, 1]], [10], [10, 0], [0])
        self.assertFalse(cert["optimality_verified"])

    def test_deada_objective_certified(self):
        model, diag = deada.fit(POOL)
        self.assertIsNotNone(model)
        self.assertTrue(diag["optimality_verified"], diag.get("reason"))
        self.assertLess(diag.get("objective_gap", 1), 1e-7)

    def test_independent_path_agrees(self):
        """两条 pivot 路径必须给出同一目标值——这是证书正确的前提。"""
        _, diag = deada.fit(POOL)
        self.assertTrue(diag["independent"]["verified"])
        self.assertLess(abs(diag["independent"]["objective"] - diag["primal_objective"])
                        if diag.get("primal_objective") is not None else 0, 1e-7)


class TestContract(unittest.TestCase):
    """契约：参数错误抛异常，数据问题返回不可用。"""

    def test_bad_params_raise(self):
        with self.assertRaises(validation.ValidationError):
            alg.assess(POOL, scenario("edge"), alpha=1.5)
        with self.assertRaises(validation.ValidationError):
            alg.assess(POOL, scenario("edge"), n_trees=0)
        with self.assertRaises(validation.ValidationError):
            alg.assess(POOL, scenario("edge"), n_trees="60")
        with self.assertRaises(validation.ValidationError):
            alg.assess(POOL, scenario("edge"), benford_applicable="yes")

    def test_bad_data_returns_unavailable(self):
        bad = dict(scenario("edge"))
        bad["X1"] = float("nan")
        r = alg.assess(POOL, bad)
        self.assertFalse(r["融合判定"]["等级"] == "绿灯")
        self.assertIn("非有限值", r["reason"])

        missing = dict(scenario("edge"))
        del missing["X7"]
        r = alg.assess(POOL, missing)
        self.assertIn("缺少指标", r["reason"])

        nonpositive = dict(scenario("edge"))
        nonpositive["X10"] = 0.0
        r = alg.assess(POOL, nonpositive)
        self.assertIn("X10", r["reason"])

    def test_tiny_pool_degrades_explicitly(self):
        r = alg.assess(ind.build_sample_pool(2), scenario("edge"))
        self.assertIsNone(r["第一层"]["Q"])
        self.assertIsNone(r["第二层"]["D"])
        self.assertIsNone(r["第三层"]["P"])
        # 全缺失时不能被说成"无风险"
        self.assertNotEqual(r["最终等级"]["等级"], "绿灯")

    def test_never_raises_on_garbage(self):
        rng = random.Random(7)
        for _ in range(30):
            bad = {k: rng.choice([float("nan"), float("inf"), 0.0, -1e200, "x", None])
                   for k in ind.KEYS}
            try:
                alg.assess(POOL, bad)
            except validation.ValidationError:
                self.fail("数据问题不应抛 ValidationError")
        for _ in range(10):
            pool = [{"label": rng.choice([0, 1]), "values": {k: rng.random() for k in ind.KEYS}}
                    for _ in range(rng.randrange(0, 12))]
            alg.assess(pool, scenario("edge"))

    def test_json_serializable(self):
        r = alg.assess(POOL, scenario("risk"), with_stability=True)
        json.dumps(r, ensure_ascii=False)


class TestEvidence(unittest.TestCase):
    """证据只升不降。"""

    def test_insufficient_evidence_never_downgrades(self):
        """P 校准不足时，D<0 且 P≥0.7 的样本必须仍是红灯。

        ra-next-0.3 的做法是把 P 从融合输入里摘掉，结果最危险样本
        从红灯降到黄灯。本版不采纳。
        """
        r = alg.assess(POOL, scenario("risk"))
        self.assertEqual(r["第三层"]["校准"]["样本充足"], False)
        self.assertEqual(r["融合判定"]["等级"], "红灯")
        self.assertEqual(r["最终等级"]["等级"], "红灯")
        self.assertTrue(r["证据"]["复核必需"])

    def test_insufficient_evidence_blocks_green(self):
        r = alg.assess(POOL, scenario("healthy"))
        self.assertEqual(r["融合判定"]["等级"], "绿灯")
        self.assertEqual(r["最终等级"]["等级"], "黄灯")

    def test_loose_mode_restores_three_bands(self):
        r = alg.assess(POOL, scenario("healthy"), require_full_evidence_for_green=False)
        self.assertEqual(r["最终等级"]["等级"], "绿灯")
        self.assertTrue(r["证据"]["复核必需"])   # 标记仍在，只是不升档

    def test_benford_true_keeps_value(self):
        r = alg.assess(POOL, scenario("risk"), benford_applicable=True)
        self.assertEqual(r["第一層"]["Q"] if False else r["第一层"]["Q"], 0.9333)
        self.assertEqual(r["证据"]["各层"]["Q"], "充分")

    def test_benford_false_drops_q(self):
        r = alg.assess(POOL, scenario("risk"), benford_applicable=False)
        self.assertEqual(r["证据"]["各层"]["Q"], "缺失")
        # 融合表对 Q 缺失有既定规则（R1），后果是红灯，调用方须清楚
        self.assertEqual(r["最终等级"]["等级"], "红灯")


class TestCache(unittest.TestCase):
    """训练/打分分离与缓存。"""

    def test_cache_hit_is_fast_and_identical(self):
        pool = ind.build_sample_pool(20)
        a = alg.assess(pool, scenario("edge"))
        t0 = time.perf_counter()
        b = alg.assess(pool, scenario("edge"))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.05, "缓存命中后单次打分应远快于构建")
        self.assertEqual(a["第一层"]["Q"], b["第一层"]["Q"])
        self.assertEqual(a["第二层"]["D"], b["第二层"]["D"])
        self.assertEqual(a["第三层"]["P"], b["第三层"]["P"])

    def test_deterministic(self):
        a = alg.assess(POOL, scenario("edge"))
        b = alg.assess(POOL, scenario("edge"))
        a.pop("耗时"), b.pop("耗时")
        self.assertEqual(json.dumps(a, ensure_ascii=False, sort_keys=True),
                         json.dumps(b, ensure_ascii=False, sort_keys=True))

    def test_different_params_build_different_models(self):
        m1 = alg.get_pool_model(POOL, n_trees=30)
        m2 = alg.get_pool_model(POOL, n_trees=60)
        self.assertNotEqual(m1.fingerprint, m2.fingerprint)
        self.assertEqual(len(m1.forest_model.trees), 30)
        self.assertEqual(len(m2.forest_model.trees), 60)

    def test_cache_eviction_bounded(self):
        for n in range(8, 14):
            alg.get_pool_model(ind.build_sample_pool(n), n_trees=20)
        self.assertLessEqual(alg.cache_stats()["当前条目"], alg.cache_stats()["上限"])


class TestPortability(unittest.TestCase):
    """可迁移性：ra-next-0.3 在目录外根本无法调用，本包必须可以。"""

    def test_import_and_call_from_anywhere(self):
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from v8 import indicators as i, algorithm as a;"
            "r = a.assess(i.build_sample_pool(20), dict(i.TARGET_SCENARIOS['edge']['values']));"
            "print(r['第二层']['D'])" % _SRC
        )
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        out = subprocess.run([sys.executable, "-c", code], cwd=os.path.expanduser("~"),
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        self.assertIn("0.187676", out.stdout)


class TestThresholdPolicy(unittest.TestCase):
    def _history(self):
        return [{"month": "2025-%02d" % i, "ratio": i / 3.0} for i in range(1, 13)]

    def test_enabled_and_static_floor(self):
        r = policy.threshold_policy(self._history(), 2.5, 1.5, "2026-01")
        self.assertTrue(r["enabled"])
        self.assertGreaterEqual(r["yellow"], 2.5)
        self.assertGreaterEqual(r["red"], 1.5)
        self.assertFalse(r["confidence"]["coverage_guaranteed"])

    def test_rejects_duplicate_future_gap_nan(self):
        h = self._history()
        self.assertFalse(policy.threshold_policy(h + h[:1], 2.5, 1.5, "2026-01")["enabled"])
        h = self._history(); h[5]["month"] = "2026-01"
        self.assertFalse(policy.threshold_policy(h, 2.5, 1.5, "2026-01")["enabled"])
        h = self._history(); h[5]["month"] = "2024-12"
        self.assertFalse(policy.threshold_policy(h, 2.5, 1.5, "2026-01")["enabled"])
        h = self._history(); h[5]["ratio"] = float("nan")
        self.assertFalse(policy.threshold_policy(h, 2.5, 1.5, "2026-01")["enabled"])
        h = self._history()
        for x in h:
            x["ratio"] = 1
        self.assertFalse(policy.threshold_policy(h, 2.5, 1.5, "2026-01")["enabled"])

    def test_insufficient_history(self):
        self.assertFalse(policy.threshold_policy(self._history()[1:], 2.5, 1.5,
                                                 "2026-01")["enabled"])

    def test_bad_params_raise(self):
        with self.assertRaises(validation.ValidationError):
            policy.threshold_policy(self._history(), 2.5, 1.5, "2026-01", window=6)
        with self.assertRaises(validation.ValidationError):
            policy.threshold_policy(self._history(), 1.5, 2.5, "2026-01")


class TestUnifiedView(unittest.TestCase):
    def test_missing_evidence_blocks_green(self):
        self.assertEqual(policy.unified_view("绿灯", "绿灯", None)["level"], "黄灯")
        self.assertEqual(policy.unified_view(0, 1, None)["level"], "黄灯")

    def test_all_missing_is_pending(self):
        out = policy.unified_view(None, None, None)
        self.assertEqual(out["level"], "待复核")
        self.assertFalse(out["complete"])

    def test_unvalidated_temporal_ignored(self):
        out = policy.unified_view("绿灯", "绿灯", 0.99)
        self.assertIn("temporal", out["missing"])

    def test_max_severity(self):
        self.assertEqual(policy.unified_view("红灯", "绿灯", 0.05,
                                             temporal_validated=True)["level"], "红灯")
        self.assertEqual(policy.unified_view("绿灯", "绿灯", 0.3,
                                             temporal_validated=True)["level"], "红灯")


class TestCalibration(unittest.TestCase):
    def test_monotone(self):
        a, b = calibration.sigmoid_fit([0.1, 0.3, 0.8, 0.9], [0, 0, 1, 1])
        prev = -1
        for p in (0.05, 0.2, 0.5, 0.8, 0.95):
            v = calibration.predict(p, a, b)
            self.assertGreaterEqual(v, prev)
            prev = v

    def test_slope_nonnegative(self):
        a, _ = calibration.sigmoid_fit([0.2, 0.4, 0.6, 0.8], [0, 1, 0, 1])
        self.assertGreaterEqual(a, 0)

    def test_degenerate_rejected(self):
        self.assertFalse(calibration.fit([0.5, 0.6], [1, 1])["available"])
        self.assertFalse(calibration.fit([0.5], [1])["available"])

    def test_brier_and_ece(self):
        pairs = [(0.1, 0), (0.2, 0), (0.8, 1), (0.9, 1)]
        self.assertAlmostEqual(calibration.brier(pairs), 0.025)
        self.assertIsNotNone(calibration.ece(pairs))
        self.assertIsNone(calibration.brier([]))


class TestQuality(unittest.TestCase):
    def test_digits(self):
        self.assertEqual(quality.first_digit(0.156), 1)
        self.assertEqual(quality.first_digit(9.8), 9)
        self.assertIsNone(quality.first_digit(0.0))
        self.assertIsNone(quality.first_digit(float("nan")))
        # 与基线一致：0 的末位按 0 计（改掉会让 Q 与历史锚点漂移）
        self.assertEqual(quality.last_digit(0.0), 0)

    def test_mc_test_validates(self):
        self.assertFalse(quality.mc_test([1], [0.5, 0.5], 199)["available"])
        self.assertFalse(quality.mc_test([1, 2], [0.5], 199)["available"])
        self.assertFalse(quality.mc_test([1, 2], [0.5, 0.6], 199)["available"])

    def test_holm(self):
        self.assertEqual(quality.holm_reject([0.001, 0.04, 0.04], 0.05), 1)
        self.assertEqual(quality.holm_reject([0.9, 0.9], 0.05), 0)

    def test_gate_always_has_q_key(self):
        for pool in ([], [{"label": 0, "values": {k: 1.0 for k in ind.KEYS}}]):
            self.assertIn("Q", quality.quality_gate(pool))


if __name__ == "__main__":
    unittest.main(verbosity=2)
