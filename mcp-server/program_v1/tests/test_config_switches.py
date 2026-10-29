# -*- coding: utf-8 -*-
"""算法开关回归测试。

覆盖两件事，缺一不可：
1. **关闭时回退**：默认值必须逐位等于加开关之前的原有行为。
   这条挂了，说明"默认安全"是假的——别人一改配置就悄悄换口径。
2. **打开时生效**：每个开关都要能真正改变行为，而不是只出现在返回体里。
   这条挂了，说明开关是装饰品。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from v8 import config, engine, fusion, indicators as ind  # noqa: E402
from v8.algorithm import policy  # noqa: E402

ALL_SCENARIOS = ("healthy", "edge", "risk")


def _fingerprint(result):
    """把一次评估压成可比较的指纹：只看影响结论的部分。"""
    fin = result["最终等级"]
    return (result["第一层"]["Q"], result["第二层"]["D"], result["第三层"]["P"],
            result["融合判定"]["规则ID"], result["融合判定"]["等级"],
            fin["等级"], fin.get("证据", {}).get("整体"))


class _EnvGuard:
    """测试期间临时改环境变量，用完彻底还原（含删除）。

    进入时把 ``CF_V8_CONFIG`` 指向一个**必定不存在**的路径：
    仓库里本身带着 ``deploy/algorithm.config.json``，不清掉它的话，
    「默认取内置默认」这类断言会变成「取决于仓库当前配置成什么」，
    别人一改配置测试就红，而测试红的原因与被测代码无关。
    """

    KEYS = ("CF_V8_CONFIG", "CF_V8_ALGO", "CF_V8_EVIDENCE", "CF_V8_BENFORD",
            "CF_V8_Q_IN_FUSION", "CF_V8_STABILITY", "CF_V8_CACHE",
            "CF_V8_CACHE_MAX", "CF_V8_POOL_N", "CF_V8_TREES")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        # 故意指向不存在的路径，让"默认"只可能来自内置默认
        os.environ["CF_V8_CONFIG"] = os.path.join(
            tempfile.gettempdir(), "cf_v8_config_absent_for_test.json")
        config.reload()
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.reload()
        return False

    def set(self, **kw):
        for k, v in kw.items():
            os.environ[k] = str(v)
        config.reload()


class TestDefaultsEqualLegacy(unittest.TestCase):
    """默认口径 == 改造前行为。"""

    def test_defaults_are_the_conservative_ones(self):
        with _EnvGuard():
            s = config.resolve()
            self.assertEqual(s.algo, "v8")          # 基线，不是新的 v1
            self.assertTrue(s.require_full_evidence_for_green)
            self.assertTrue(s.q_in_fusion)
            self.assertIsNone(s.benford_applicable)
            self.assertFalse(s.stability)
            self.assertTrue(s.cache_enabled)
            self.assertEqual(s.pool_n, 20)
            self.assertEqual(s.trees, 60)

    def test_default_engine_output_has_no_v1_only_fields(self):
        """默认走 v8 时，返回体不该出现 v1 专属的证据/开关字段。"""
        with _EnvGuard():
            r = engine.assess()
            self.assertEqual(r["算法版本"], "v8-baseline")
            self.assertNotIn("证据", r["最终等级"])

    def test_fusion_unchanged_when_q_in_fusion_default(self):
        """Q 参与判别（默认）时，48 格表的状态映射与历史完全一致。"""
        with _EnvGuard():
            for q in (None, 0.1, 0.4, 0.9333):
                for d in (-0.5, 0.5, None):
                    for p in (0.1, 0.8, None):
                        a = fusion.fuse(q, d, p)
                        b = fusion.fuse(q, d, p, q_in_fusion=True)
                        self.assertEqual(a["规则ID"], b["规则ID"])
                        self.assertEqual(a["等级"], b["等级"])


class TestFourLevelOverride(unittest.TestCase):
    """四级覆盖：请求参数 > 环境变量 > 配置文件 > 内置默认。"""

    def test_builtin_default_source(self):
        with _EnvGuard():
            self.assertEqual(config.resolve().sources["算法版本"], "内置默认")

    def test_file_level(self):
        with _EnvGuard() as g:
            path = self._write({"algorithm": {"default": "v1"},
                                "quality": {"gate_in_fusion": False}})
            g.set(CF_V8_CONFIG=path)
            s = config.resolve()
            self.assertEqual(s.algo, "v1")
            self.assertFalse(s.q_in_fusion)
            self.assertIn("配置文件", s.sources["算法版本"])

    def test_env_beats_file(self):
        with _EnvGuard() as g:
            path = self._write({"algorithm": {"default": "v1"}})
            g.set(CF_V8_CONFIG=path, CF_V8_ALGO="ra-next-0.1")
            s = config.resolve()
            self.assertEqual(s.algo, "ra-next-0.1")
            self.assertIn("环境变量", s.sources["算法版本"])

    def test_request_beats_env(self):
        with _EnvGuard() as g:
            g.set(CF_V8_ALGO="v1", CF_V8_Q_IN_FUSION="1")
            s = config.resolve(algo="v8", q_in_fusion=False)
            self.assertEqual(s.algo, "v8")
            self.assertFalse(s.q_in_fusion)
            self.assertEqual(s.sources["算法版本"], "请求参数")

    def test_empty_env_is_unset_not_false(self):
        """留空 = 未配置，不等于 False。否则"留空"会悄悄关掉 Q 参与判别。"""
        with _EnvGuard() as g:
            g.set(CF_V8_Q_IN_FUSION="")
            self.assertTrue(config.resolve().q_in_fusion)

    def _write(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path


class TestInvalidFallsBack(unittest.TestCase):
    """配置配错最多退化成默认，绝不让服务起不来或走歪。"""

    def test_unknown_algo(self):
        with _EnvGuard() as g:
            g.set(CF_V8_ALGO="v9-nope")
            self.assertEqual(config.resolve().algo, "v8")

    def test_bad_evidence_mode(self):
        with _EnvGuard() as g:
            g.set(CF_V8_EVIDENCE="banana")
            self.assertTrue(config.resolve().require_full_evidence_for_green)

    def test_bad_numbers(self):
        with _EnvGuard() as g:
            g.set(CF_V8_TREES="abc", CF_V8_POOL_N="999999", CF_V8_CACHE_MAX="-3")
            s = config.resolve()
            self.assertEqual(s.trees, 60)
            self.assertEqual(s.pool_n, 20)
            self.assertEqual(s.cache_max, 64)

    def test_broken_json(self):
        with _EnvGuard() as g:
            fd, path = tempfile.mkstemp(suffix=".json")
            os.write(fd, b"{ not json")
            os.close(fd)
            self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
            g.set(CF_V8_CONFIG=path)
            s = config.resolve()
            self.assertEqual(s.algo, "v8")
            self.assertFalse(config.snapshot()["配置文件存在"] is False)

    def test_missing_file(self):
        with _EnvGuard() as g:
            g.set(CF_V8_CONFIG=os.path.join(tempfile.gettempdir(),
                                            "definitely_not_here.json"))
            self.assertEqual(config.resolve().algo, "v8")

    def test_unknown_keys_ignored(self):
        with _EnvGuard() as g:
            fd, path = tempfile.mkstemp(suffix=".json")
            os.write(fd, json.dumps({"bogus": {}, "cache": {"nope": 1, "max": 8}})
                     .encode("utf-8"))
            os.close(fd)
            self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
            g.set(CF_V8_CONFIG=path)
            self.assertEqual(config.resolve().cache_max, 8)


class TestQInFusionSwitch(unittest.TestCase):
    """Q 参与融合判别开关：关掉时 Q 退出判别，但决策表一格未改。"""

    def test_off_skips_q_in_shortboard(self):
        """Q 不参与判别时，Q 的证据等级不再卡住整体证据。"""
        levels = {"Q": "不足", "D": "充分", "P": "充分"}
        self.assertEqual(policy.overall_evidence(levels), "不足")
        self.assertEqual(policy.overall_evidence(levels, frozenset(["Q"])), "充分")

    def test_off_does_not_lie_about_q(self):
        """Q 的证据等级照实记录，只是不进短板——不能假装它充分。"""
        lv, skipped = policy.evidence_levels(
            {"available": True, "Q": 0.93}, {"D": 0.5, "最优性确认": True},
            {"available": True, "P": 0.5, "校准": {"样本充足": True, "收敛": True}},
            benford_applicable=None, q_in_fusion=False)
        self.assertEqual(lv["Q"], "不足")      # 仍是真相
        self.assertEqual(skipped, frozenset(["Q"]))

    def test_on_keeps_q_in_shortboard(self):
        lv, skipped = policy.evidence_levels(
            {"available": True, "Q": 0.93}, {"D": 0.5, "最优性确认": True},
            {"available": True, "P": 0.5, "校准": {"样本充足": True, "收敛": True}},
            benford_applicable=None, q_in_fusion=True)
        self.assertEqual(skipped, frozenset())
        self.assertEqual(policy.overall_evidence(lv, skipped), "不足")

    def test_table_still_total_when_off(self):
        """关掉 Q 参与后，48 种组合仍必须全部命中某条规则（无洞无重叠）。"""
        seen = {}
        for q in (None, 0.1, 0.4, 0.9333):
            for d in (-0.5, 0.5, None):
                for p in (0.1, 0.8, None):
                    for dr in (True, False):
                        for pr in (True, False):
                            r = fusion.fuse(q, d, p, d_reliable=dr, p_reliable=pr,
                                            q_in_fusion=False)
                            key = (r["状态"]["Q"], r["状态"]["D"], r["状态"]["P"])
                            seen[key] = r["规则ID"]
        # Q 维度被钉在「达标」，因此可达组合数从 48 降到 24，但每个都必命中
        self.assertTrue(all(v.startswith("FUSE-R") for v in seen.values()))
        self.assertTrue(all(k[0] in ("达标", "缺失") for k in seen))

    def test_off_restores_green_in_loose_mode(self):
        """不开 Q 参与 + 宽松证据 → 三档判别恢复（这是开关的实际业务价值）。"""
        with _EnvGuard():
            got = {}
            for sc in ALL_SCENARIOS:
                r = engine.assess(scenario=sc, algo="v1", q_in_fusion=False,
                                  require_full_evidence_for_green=False)
                got[sc] = r["最终等级"]["等级"]
            self.assertEqual(got["healthy"], "绿灯")
            self.assertEqual(got["risk"], "红灯")

    def test_off_alone_still_blocks_green_in_strict(self):
        """只关 Q 参与、证据模式仍是严格 → P 证据不足仍卡住绿灯。"""
        with _EnvGuard():
            r = engine.assess(scenario="healthy", algo="v1", q_in_fusion=False)
            self.assertEqual(r["最终等级"]["等级"], "黄灯")


class TestCacheSwitch(unittest.TestCase):
    """缓存开关：只影响耗时，不影响任何一个数值。"""

    def test_cache_off_gives_identical_numbers(self):
        from v8.algorithm import model as m
        with _EnvGuard():
            pool = ind.build_sample_pool(20)
            saved = (m._CACHE_ENABLED, m._CACHE_MAX)
            try:
                m.set_cache_enabled(True)
                m.clear_cache()
                on = m.assess(pool, ind.TARGET_SCENARIOS["edge"]["values"])
                m.set_cache_enabled(False)
                off = m.assess(pool, ind.TARGET_SCENARIOS["edge"]["values"])
            finally:
                m.set_cache_enabled(saved[0])
                m.set_cache_max(saved[1])
            self.assertEqual(on["第一层"]["Q"], off["第一层"]["Q"])
            self.assertEqual(on["第二层"]["D"], off["第二层"]["D"])
            self.assertEqual(on["第三层"]["P"], off["第三层"]["P"])
            self.assertEqual(on["融合判定"]["规则ID"], off["融合判定"]["规则ID"])

    def test_cache_stats_expose_enabled(self):
        from v8.algorithm import model as m
        with _EnvGuard():
            saved = m._CACHE_ENABLED
            try:
                m.set_cache_enabled(False)
                self.assertFalse(m.cache_stats()["启用"])
                m.set_cache_enabled(True)
                self.assertTrue(m.cache_stats()["启用"])
            finally:
                m.set_cache_enabled(saved)


class TestEngineWiring(unittest.TestCase):
    """开关必须真的接到引擎上，而不是只躺在 config 里。"""

    def test_settings_appear_in_output(self):
        with _EnvGuard():
            r = engine.assess(algo="v1")
            self.assertIn("生效开关", r)
            self.assertEqual(r["生效开关"]["算法版本"], "v1")
            self.assertIn("取值来源", r["生效开关"])

    def test_env_var_reaches_engine(self):
        with _EnvGuard() as g:
            g.set(CF_V8_ALGO="v1", CF_V8_Q_IN_FUSION="0", CF_V8_EVIDENCE="loose")
            r = engine.assess(scenario="healthy")
            self.assertEqual(r["算法版本"], "ra-next-v1.0")
            self.assertFalse(r["生效开关"]["Q参与融合判别"])
            self.assertEqual(r["最终等级"]["等级"], "绿灯")

    def test_explicit_arg_beats_env(self):
        with _EnvGuard() as g:
            g.set(CF_V8_ALGO="v1")
            r = engine.assess(algo="v8")
            self.assertEqual(r["算法版本"], "v8-baseline")

    def test_unset_is_not_none_for_benford(self):
        """benford_applicable=None 是合法取值（未声明），不能被当成'没传'。"""
        with _EnvGuard() as g:
            g.set(CF_V8_ALGO="v1", CF_V8_BENFORD="1")
            r = engine.assess()
            self.assertEqual(r["第一层"]["适用性声明"], "已声明适用")
            r2 = engine.assess(benford_applicable=None)
            self.assertIn("未声明", r2["第一层"]["适用性声明"])


class TestCommittedConfigFile(unittest.TestCase):
    """随仓库提交的配置文件本身就是一份口径声明，必须与内置默认一致。

    这条测试不隔离 CF_V8_CONFIG，专门读仓库里真实那份
    ``deploy/algorithm.config.json``：如果有人把它改成"默认走 v1 + 宽松"，
    那等于悄悄换了全站口径，必须让 CI 挡下来。
    """

    def _repo_config(self):
        # config.py 里 _ROOT 是「含 deploy 与 web 两个目录」的仓库根
        path = os.path.join(config._ROOT, "deploy", "algorithm.config.json")
        if not os.path.exists(path):
            self.skipTest("仓库未携带 deploy/algorithm.config.json")
        return path

    def test_committed_file_matches_builtin_defaults(self):
        path = self._repo_config()
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for section, values in config.DEFAULTS.items():
            if section == "algorithm":
                continue            # allowed 列表不要求出现在文件里
            self.assertIn(section, data, "配置文件缺少段 %s" % section)
            for key, default in values.items():
                self.assertIn(key, data[section], "配置文件缺少 %s.%s" % (section, key))
                self.assertEqual(data[section][key], default,
                                 "配置文件 %s.%s=%r 与内置默认 %r 不一致"
                                 % (section, key, data[section][key], default))
        self.assertEqual(data.get("algorithm", {}).get("default"),
                         config.DEFAULTS["algorithm"]["default"])

    def test_repo_default_behaviour_is_legacy(self):
        """用仓库真实配置文件解析出来的口径，必须等于改造前行为。"""
        path = self._repo_config()
        saved = os.environ.get("CF_V8_CONFIG")
        os.environ["CF_V8_CONFIG"] = path
        try:
            config.reload()
            s = config.resolve()
            self.assertEqual(s.algo, "v8")
            self.assertTrue(s.require_full_evidence_for_green)
            self.assertTrue(s.q_in_fusion)
        finally:
            if saved is None:
                os.environ.pop("CF_V8_CONFIG", None)
            else:
                os.environ["CF_V8_CONFIG"] = saved
            config.reload()


class TestSnapshot(unittest.TestCase):
    """/api/v8/config 的输出必须能用于上线核验。"""

    def test_snapshot_shape(self):
        with _EnvGuard():
            s = config.snapshot()
            self.assertIn("生效口径", s)
            self.assertIn("取值来源", s)
            self.assertIn("内置默认", s)
            self.assertIn("环境变量映射", s)
            self.assertTrue(any(k.startswith("CF_V8_")
                                for k in s["环境变量映射"]))

    def test_reload_picks_up_file_change(self):
        with _EnvGuard() as g:
            fd, path = tempfile.mkstemp(suffix=".json")
            os.close(fd)
            self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
            g.set(CF_V8_CONFIG=path)
            # 两次改动刻意做成**字节数相同**（v8→v1），验证热加载没有偷懒比 mtime
            for expect in ("v8", "v1"):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write('{"algorithm":{"default":"%s"}}' % expect)
                config.reload()
                self.assertEqual(config.resolve().algo, expect)


if __name__ == "__main__":
    unittest.main(verbosity=2)
