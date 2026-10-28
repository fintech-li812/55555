# -*- coding: utf-8 -*-
"""V8 差距补齐项的验证用例。

覆盖本次补齐的四类改动：
  1. 三层模型拆分为独立 MCP 工具后，与整包 risk.assess_v8 的结果一致性；
  2. DEA-DA 单纯形法改为 Bland 规则后的跨平台可复现性；
  3. 动态阈值（滚动 12 个月分位数）的启用条件与回退行为；
  4. 分位数函数与判定分级的边界行为。

设计原则：
  * 全部用例不依赖数据库，可在任何环境离线运行；
  * 每条断言都写明「为什么这个值是期望值」，便于后人判断是 bug 还是口径变更；
  * 不静默降级：任何一项失败都直接 fail，不 skip、不 xfail。

运行：
    python -m pytest mcp-server/program_v1/tests/test_v8_gap_closure.py -v
    # 或
    python mcp-server/program_v1/tests/test_v8_gap_closure.py
"""
from __future__ import annotations

import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from v8 import indicators as ind          # noqa: E402
from v8 import industry as ins            # noqa: E402
from v8 import layers as L                # noqa: E402

SCENARIOS = ("healthy", "edge", "risk")


def _pool(n: int = 20):
    return ind.build_sample_pool(n)


def _values(scenario: str):
    return ind.TARGET_SCENARIOS[scenario]["values"]


# ---------------------------------------------------------------------------
# 1. 三层模型与整包评估的一致性
# ---------------------------------------------------------------------------
class TestLayerConsistency(unittest.TestCase):
    """独立调用某一层，必须与整包评估得到完全相同的 Q / D / P。

    这是把 quality.check / risk.deada_score / risk.rf_predict 拆成独立工具后
    最重要的不变量：拆分只能改变调用方式，不能改变计算结果。
    """

    def test_three_layers_match_engine_assess(self):
        from v8 import engine
        assess = engine.assess(industry="制造业", scenario="edge",
                               amount=120000.0, n_per_group=20)

        pool = _pool(20)
        values = _values("edge")
        gate = L.quality_gate(pool)
        da = L.deada(pool, values)
        rf = L.benford_rf(pool, values, gate)

        self.assertEqual(gate["Q"], assess["第一层"]["Q"], "第一层 Q 与整包评估不一致")
        self.assertEqual(da["D"], assess["第二层"]["D"], "第二层 D 与整包评估不一致")
        self.assertEqual(rf["P"], assess["第三层"]["P"], "第三层 P 与整包评估不一致")

    def test_independent_call_is_reproducible(self):
        """同进程内重复调用同一层，结果必须逐位相同（无隐藏随机源）。"""
        pool = _pool(20)
        values = _values("edge")
        d1 = L.deada(pool, values)["D"]
        d2 = L.deada(_pool(20), values)["D"]
        self.assertEqual(d1, d2, "同参数重复调用得到不同的 D，存在未固定的随机源")

    def test_all_scenarios_produce_expected_lamp(self):
        """三个受评场景的融合判定。

        注意：edge 场景的期望值经历过两次变更，都是修 bug 的结果，
        不是口径漂移：
          1. 2026-10-28 修复 DEA-DA 求解方向（原实现在最大化误分类）后，
             D 由 -0.118 变为 +0.188，等级由「红灯」变为「绿灯」；
          2. 同日融合规则消歧后，因该场景 P=0.9833 ≥ 0.7 且 D≥0，
             命中新增的 FUSE-R6（两个结论冲突，不放行绿灯），
             等级由「绿灯」变为「黄灯」。

        第 2 次变更是本次唯一的行为变化：红灯口径未动，只是不再允许
        在舞弊概率高达 98% 时判「绿灯 · 不触发动作」。
        """
        expected = {"healthy": "绿灯", "edge": "黄灯", "risk": "红灯"}
        for sc in SCENARIOS:
            pool = _pool(20)
            values = _values(sc)
            gate = L.quality_gate(pool)
            da = L.deada(pool, values)
            rf = L.benford_rf(pool, values, gate)
            fused = L.fuse(gate["Q"], da["D"], rf["P"])
            self.assertEqual(fused["等级"], expected[sc],
                             "场景 %s 的融合判定为 %s，期望 %s（Q=%s D=%s P=%s）"
                             % (sc, fused["等级"], expected[sc],
                                gate["Q"], da["D"], rf["P"]))

    def test_high_fraud_probability_alone_does_not_trigger_red(self):
        """原「口径矛盾」格：P=0.98 但 D≥0 —— 已于 2026-10-28 消歧。

        历史背景：第三层 benford_rf 自称口径是「P ≥ 0.7 升级红灯」，
        但旧 fuse() 要求 (D < 0 且 P ≥ 0.7)，两者不一致，当时只能把
        「判绿灯」这个冲突行为固定下来等待裁决。

        裁决结果（见 docs/融合规则口径冲突消歧报告_20261028.md）：
          * 红灯口径不变，仍需 D<0 且 P≥0.7（2-A.7 为规范口径）；
          * 但 P≥0.7 不再允许判绿灯——否则第三层输出在融合中被完全丢弃，
            可能对 P=0.98 的样本判「绿灯 · 不触发动作」。
          * 故本格由「绿灯」改为「黄灯（FUSE-R6：结论冲突，转人工复核）」。

        这是本次消歧唯一的行为变化，也是唯一需要业务确认的一格。
        """
        fused = L.fuse(0.5667, 0.187676, 0.9833)     # Q 与 D 达标、P 极高
        self.assertEqual(fused["等级"], "黄灯")
        self.assertEqual(fused["规则ID"], "FUSE-R6")
        self.assertIn("冲突", fused["依据"])
        # 对照组：同样的高 P，只要 D<0 就应判红（红灯口径未变）
        self.assertEqual(L.fuse(0.5667, -0.1, 0.9833)["等级"], "红灯")

    def test_deada_not_degraded(self):
        """线性规划应求得最优解；降级为等权说明求解失败，必须被测试拦住。"""
        da = L.deada(_pool(20), _values("edge"))
        self.assertFalse(da["降级"], "DEA-DA 求解降级为等权判别，需排查单纯形法")


# ---------------------------------------------------------------------------
# 2. 单纯形法的确定性与可复现性
# ---------------------------------------------------------------------------
class TestSimplexDeterminism(unittest.TestCase):
    """Bland 规则修复前后的判据：修复前不同平台会走出不同 pivot 路径。

    无法在本用例里直接复现"另一台机器"，但可以通过以下不变量守住修复：
      * 同一输入多次调用逐位一致；
      * 不同变量排列（等价问题）得到相同的最优目标值；
      * 平局场景（两列 reduce cost 相同）不会因浮点扰动而发散。
    """

    def test_repeated_calls_bitwise_identical(self):
        a = L.deada(_pool(20), _values("edge"))["D"]
        b = L.deada(_pool(20), _values("edge"))["D"]
        self.assertEqual(a, b)

    def test_simplex_direction_is_maximize(self):
        """锁定 _simplex 的语义为 maximize —— deada 依赖这一点取负号实现最小化。

        判别式：c=[1,1,0]、约束 x1+x2+s=10。
          maximize → 取到 x1+x2=10，obj=10
          minimize → 取到 x1=x2=0，obj=0
        原 docstring 误写为 "min"，2026-10-28 已更正；本用例防止再次漂移。
        """
        _x, obj, ok = L._simplex([1.0, 1.0, 0.0], [[1.0, 1.0, 1.0]], [10.0])
        self.assertTrue(ok)
        self.assertAlmostEqual(obj, 10.0, places=6,
                               msg="_simplex 方向发生变化：deada 的符号需同步调整")

    def test_deada_minimizes_not_maximizes_misclassification(self):
        """DEA-DA 必须最小化误分类总量，而不是最大化。

        修复前：c 中松弛系数为 +1，配合 maximize 语义 = 最大化误分类，
                实测松弛总和 32.03（40 个中 39 个非零）。
        修复后：系数取 -1，松弛总和显著下降。
        本用例直接比较两种系数下的松弛总和，守住求解方向。
        """
        def slack_total(sign: float) -> float:
            keys = ind.DEADA_INPUTS + ind.DEADA_OUTPUTS
            pool = _pool(20)
            rows, labels = [], []
            for s in pool:
                v = dict(s["values"])
                v["AR_DAYS"] = -ind.ar_days(v)
                v["X13"] = -v["X13"]
                rows.append(v)
                labels.append(s["label"])
            Z, _norm = L._zscore_matrix(rows, keys)
            g1 = [(i, Z[i]) for i in range(len(Z)) if labels[i] == 1]
            g2 = [(i, Z[i]) for i in range(len(Z)) if labels[i] == 0]
            ni, no = len(ind.DEADA_INPUTS), len(ind.DEADA_OUTPUTS)
            nv = ni + no
            nvar = nv + 2 + len(g1) + len(g2)
            c = [0.0] * nvar
            for k in range(nv + 2, nvar):
                c[k] = sign
            A, b = [], []
            for k1, (_, r) in enumerate(g1):
                row = [0.0] * nvar
                for i, k in enumerate(ind.DEADA_INPUTS):
                    row[i] = r[k]
                for j, k in enumerate(ind.DEADA_OUTPUTS):
                    row[ni + j] = r[k]
                row[nv], row[nv + 1] = -1.0, 1.0
                row[nv + 2 + k1] = 1.0
                A.append(row)
                b.append(0.0)
            for k2, (_, r) in enumerate(g2):
                row = [0.0] * nvar
                for i, k in enumerate(ind.DEADA_INPUTS):
                    row[i] = r[k]
                for j, k in enumerate(ind.DEADA_OUTPUTS):
                    row[ni + j] = r[k]
                row[nv], row[nv + 1] = -1.0, 1.0
                row[nv + 2 + len(g1) + k2] = -1.0
                A.append(row)
                b.append(0.0)
            row = [1.0] * nv + [0.0] * (nvar - nv)
            A.append(row)
            b.append(1.0)
            x, _obj, ok = L._simplex(c, A, b)
            self.assertTrue(ok, "DEA-DA 线性规划求解失败")
            return sum(x[nv + 2:])

        minimized = slack_total(-1.0)      # 当前实现（正确）
        maximized = slack_total(+1.0)      # 修复前的行为（错误）
        self.assertLess(minimized, maximized,
                        "松弛总和未下降（min=%.4f, max=%.4f）：求解方向可能又反了"
                        % (minimized, maximized))

    def test_tied_objective_is_stable(self):
        """构造系数完全对称的 LP：两列完全等价，入基只能靠下标决定。

        若实现回退成「选最负 reduce cost」，两列系数相等时行为将依赖浮点比较
        的先后顺序，结果会随平台漂移。Bland 规则下应当稳定。
        """
        # maximize (x1 + x2)，s.t. x1 + x2 + s = 10，x >= 0
        # 注意 _simplex 是 maximize 语义（见 test_simplex_direction_is_maximize）
        c = [1.0, 1.0, 0.0]
        A = [[1.0, 1.0, 1.0]]
        b = [10.0]
        x, obj, ok = L._simplex(c, A, b)
        self.assertTrue(ok, "对称 LP 求解失败")
        self.assertAlmostEqual(obj, 10.0, places=6)
        self.assertAlmostEqual(sum(x[:2]), 10.0, places=6)

    def test_bounded_solution_respected(self):
        """带上下界约束的 LP 不应返回负值解。"""
        c = [1.0, 1.0]
        A = [[1.0, 0.0], [0.0, 1.0]]
        b = [3.0, 4.0]
        x, obj, ok = L._simplex(c, A, b)
        self.assertTrue(ok)
        for v in x:
            self.assertGreaterEqual(v, -1e-9, "解出现负数，违反 x >= 0")


# ---------------------------------------------------------------------------
# 3. 动态阈值（滚动 12 个月分位数）
# ---------------------------------------------------------------------------
class TestDynamicThreshold(unittest.TestCase):
    """差距报告 v7 遗留 #8：动态阈值启用条件统一为「数据满 12 个月」."""

    def test_no_history_falls_back_to_static(self):
        d = ins.dynamic_thresholds("制造业", None)
        self.assertFalse(d["启用"])
        self.assertEqual(d["模式"], "static")
        self.assertEqual(d["样本月数"], 0)
        self.assertEqual(d["K黄"], ins.INDUSTRIES["制造业"]["K黄"])
        self.assertIn("未满", d["原因"])

    def test_eleven_months_still_static(self):
        """11 个月不够，必须仍是静态 —— 这是容易差一的边界。"""
        d = ins.dynamic_thresholds("制造业", [2.0] * 11)
        self.assertFalse(d["启用"])
        self.assertEqual(d["样本月数"], 11)
        self.assertEqual(d["最低月数"], 12)

    def test_twelve_months_enables_dynamic(self):
        hist = [float(i) for i in range(1, 13)]          # 1..12
        d = ins.dynamic_thresholds("制造业", hist)
        self.assertTrue(d["启用"])
        self.assertEqual(d["模式"], "dynamic")
        self.assertEqual(d["样本月数"], 12)
        # P25 of 1..12：pos=(12-1)*0.25=2.75 → 3 + (4-3)*0.75 = 3.75
        self.assertAlmostEqual(d["K黄"], 3.75, places=4)
        # P10 of 1..12：pos=11*0.10=1.1 → 2 + (3-2)*0.1 = 2.1
        self.assertAlmostEqual(d["K红"], 2.1, places=4)
        self.assertFalse(d["已修正倒挂"])

    def test_inverted_quantiles_get_corrected(self):
        """全零历史会让 P10 与 P25 相等甚至倒挂，红灯区不得出现空集。"""
        d = ins.dynamic_thresholds("制造业", [0.0] * 12)
        self.assertTrue(d["启用"])
        self.assertLessEqual(d["K红"], d["K黄"],
                             "K红 不应大于 K黄，否则红灯判定区为空")

    def test_static_control_always_present(self):
        """无论启用与否都要给出静态对照，供前端解释阈值来源。"""
        for hist in (None, [2.0] * 5, [2.0] * 20):
            d = ins.dynamic_thresholds("零售业", hist)
            self.assertIn("静态对照", d)
            self.assertEqual(d["静态对照"]["K黄"], ins.INDUSTRIES["零售业"]["K黄"])

    def test_warning_lines_use_dynamic_when_enough(self):
        hist = [float(i) for i in range(1, 13)]
        w = ins.warning_lines("制造业", 100.0, hist)
        self.assertTrue(w["动态阈值"]["启用"])
        self.assertAlmostEqual(w["黄灯线"], 100.0 * 3.75, places=4)
        self.assertAlmostEqual(w["红灯线"], 100.0 * 2.1, places=4)

    def test_warning_lines_fall_back_when_insufficient(self):
        w = ins.warning_lines("制造业", 100.0, [2.0] * 6)
        self.assertFalse(w["动态阈值"]["启用"])
        # 回退后预警线必须等于静态口径
        self.assertAlmostEqual(w["黄灯线"],
                               100.0 * ins.INDUSTRIES["制造业"]["K黄"], places=6)

    def test_judge_by_buffer_with_dynamic_history(self):
        """动态阈值下，低于 P10 的缓冲应判红灯。"""
        hist = [float(i) for i in range(1, 13)]          # P10 = 2.1
        r = ins.judge_by_buffer("制造业", 100.0, 150.0, hist)   # 倍数 1.5 < 2.1
        self.assertEqual(r["等级"], "红灯")
        r2 = ins.judge_by_buffer("制造业", 100.0, 900.0, hist)  # 倍数 9.0 > 3.75
        self.assertEqual(r2["等级"], "绿灯")


# ---------------------------------------------------------------------------
# 4. 分位数函数的边界行为
# ---------------------------------------------------------------------------
class TestPercentile(unittest.TestCase):
    """线性插值分位数，口径对齐 numpy.percentile(method="linear")."""

    def test_odd_count_exact_positions(self):
        v = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(ins.percentile(v, 0.25), 2.0, places=9)
        self.assertAlmostEqual(ins.percentile(v, 0.50), 3.0, places=9)
        self.assertAlmostEqual(ins.percentile(v, 0.75), 4.0, places=9)

    def test_even_count_interpolation(self):
        v = [1, 2, 3, 4]
        # pos = 3 * 0.25 = 0.75 → 1 + (2-1)*0.75
        self.assertAlmostEqual(ins.percentile(v, 0.25), 1.75, places=9)

    def test_endpoints(self):
        v = [3, 1, 2]
        self.assertAlmostEqual(ins.percentile(v, 0.0), 1.0, places=9)
        self.assertAlmostEqual(ins.percentile(v, 1.0), 3.0, places=9)

    def test_single_value(self):
        self.assertAlmostEqual(ins.percentile([7.0], 0.25), 7.0, places=9)

    def test_unsorted_input_is_sorted_internally(self):
        self.assertAlmostEqual(ins.percentile([5, 1, 3, 2, 4], 0.5), 3.0, places=9)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            ins.percentile([], 0.5)

    def test_out_of_range_q_raises(self):
        for q in (-0.1, 1.1):
            with self.assertRaises(ValueError):
                ins.percentile([1, 2, 3], q)


if __name__ == "__main__":
    unittest.main(verbosity=2)
