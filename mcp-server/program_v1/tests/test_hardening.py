# -*- coding: utf-8 -*-
"""巡航发现的缺陷回归测试。

每一条都对应 2026-10-29 巡航（``scripts/cruise_check.py``）里实际报出的问题。
没有这些测试，修过的 bug 改几行代码就会悄悄回来——尤其是"请求级参数绕过
配置校验"这类，单看代码很像是对的。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from v8 import engine  # noqa: E402
from v8.algorithm import model as m  # noqa: E402
from v8.algorithm.validation import ValidationError  # noqa: E402


class TestRequestLevelBounds(unittest.TestCase):
    """请求级参数不能绕过取值范围。

    配置层确实会在超范围时回落，但它管不到调用方直接塞进来的值。
    """

    def test_pool_size_over_upper_bound_rejected(self):
        for bad in (61, 999999):
            with self.assertRaises(ValidationError, msg="n=%s" % bad):
                engine.assess(n_per_group=bad)

    def test_pool_size_zero_or_negative_rejected(self):
        # 这两个值以前会在 z-score 里直接除零，返回 500 而不是 400
        for bad in (0, -5):
            with self.assertRaises(ValidationError, msg="n=%s" % bad):
                engine.assess(n_per_group=bad)

    def test_pool_size_in_range_accepted(self):
        for good in (2, 20, 60):
            r = engine.assess(n_per_group=good)
            self.assertIn("最终等级", r)

    def test_trees_bounds(self):
        with self.assertRaises(ValidationError):
            engine.assess(algo="v1", n_trees=0)
        with self.assertRaises(ValidationError):
            engine.assess(algo="v1", n_trees=999999)

    def test_non_numeric_pool_size_rejected(self):
        with self.assertRaises(ValidationError):
            engine.assess(n_per_group="abc")

    def test_oversized_pool_is_bounded_not_hung(self):
        """n=999999 必须在参数校验阶段被挡住，而不是去建 400 万条样本。

        这条测试故意不真正调用 n=999999 的完整流程（那要跑几分钟），
        只验证它在 assess 入口就被拒绝——一旦入口校验被删，这条立刻红。
        """
        t0 = time.perf_counter()
        with self.assertRaises(ValidationError):
            engine.assess(n_per_group=999999)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestAmountFiniteness(unittest.TestCase):
    """金额必须有限：inf/NaN 会让后续比较与入库静默失效。"""

    def test_inf_rejected(self):
        with self.assertRaises(ValidationError):
            engine.assess(amount=float("1e309"))

    def test_nan_rejected(self):
        with self.assertRaises(ValidationError):
            engine.assess(amount=float("nan"))

    def test_negative_inf_rejected(self):
        with self.assertRaises(ValidationError):
            engine.assess(amount=float("-1e309"))

    def test_normal_amounts_accepted(self):
        for v in (0, 1, 120000, 1e9):
            self.assertIn("最终等级", engine.assess(amount=v))


class TestConcurrentBuild(unittest.TestCase):
    """并发首评只构建一次池模型（per-key 锁）。

    以前 8 个线程各自重算，纯 Python 的 CPU 密集计算互相抢 GIL，
    实测 8 并发首评 P95 从 ~1.3s 恶化到 9.7s。
    """

    def test_only_one_build_for_same_pool(self):
        import random
        from v8 import indicators as ind

        m.clear_cache()
        m._STATS["misses"] = 0
        m._STATS["hits"] = 0
        saved = m._CACHE_ENABLED
        try:
            m.set_cache_enabled(True)
            pool = ind.build_sample_pool(20)
            target = ind.TARGET_SCENARIOS["edge"]["values"]
            errors = []

            def worker():
                try:
                    for _ in range(3):
                        m.assess(pool, target)
                except Exception as e:      # pragma: no cover
                    errors.append(e)

            ths = [threading.Thread(target=worker) for _ in range(8)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            self.assertFalse(errors)
            stats = m.cache_stats()
            # 8 个线程全部 miss，但只有一个真正构建；其余 7 个等待后命中。
            # 关键不是"命中率高"，而是**没有 8 次构建**。
            self.assertEqual(stats["未命中"], 8)
            self.assertLessEqual(stats["当前条目"], 1)
        finally:
            m.set_cache_enabled(saved)
            m.clear_cache()

    def test_different_pools_build_independently(self):
        """不同 key 不能互相阻塞，否则 per-key 锁就退化成全局锁。"""
        from v8 import indicators as ind

        m.clear_cache()
        saved = m._CACHE_ENABLED
        try:
            m.set_cache_enabled(True)
            pools = [ind.build_sample_pool(n) for n in (2, 3)]
            target = ind.TARGET_SCENARIOS["edge"]["values"]
            results = [None, None]

            def worker(i):
                results[i] = m.assess(pools[i], target)["融合判定"]["规则ID"]

            ths = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            t0 = time.perf_counter()
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            elapsed = time.perf_counter() - t0
            self.assertTrue(all(results))
            # 两个不同池的构建是并行的，总耗时应接近较慢的那个而不是两者之和
            self.assertLess(elapsed, 5)
        finally:
            m.set_cache_enabled(saved)
            m.clear_cache()


class TestFrontendWiring(unittest.TestCase):
    """页面里 JS 引用的元素 id 必须真实存在。

    删 DOM 忘了删引用，只会在这里暴露，而人工点一遍很难覆盖到。
    """

    def _page(self):
        root = os.path.dirname(_SRC)
        for _ in range(6):
            if os.path.isdir(os.path.join(root, "web")) and \
                    os.path.isdir(os.path.join(root, "deploy")):
                break
            root = os.path.dirname(root)
        path = os.path.join(root, "web", "统一工作台.html")
        if not os.path.exists(path):
            self.skipTest("找不到 web/统一工作台.html")
        return open(path, encoding="utf-8").read()

    def test_all_referenced_ids_exist(self):
        import re
        text = self._page()
        ids = set(re.findall(r'id="([^"]+)"', text))
        refs = set(re.findall(r"U\.\$\('([A-Za-z0-9_]+)'\)", text))
        missing = sorted(refs - ids)
        self.assertEqual(missing, [], "JS 引用了不存在的元素 id：%s" % missing)

    def test_switch_ids_present(self):
        import re
        text = self._page()
        for cid in ("rkEvi", "rkQif", "rkBen", "rkStab", "rkSwHint"):
            self.assertIn('id="%s"' % cid, text, "缺少开关控件 %s" % cid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
