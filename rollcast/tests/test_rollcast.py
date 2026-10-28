'''Tests for the rolling forecast package.

Run:  python -m unittest -v rollcast.tests.test_rollcast
'''
from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rollcast.config import Config, load_config
from rollcast.core import (
    HORIZON,
    RollingState,
    detect_seasonality,
    resolve_bias,
    select_block_length,
    simulate_paths,
    summarise_paths,
    walk_forward_backtest,
)
from rollcast.pipeline import StageError, run_pipeline
from rollcast.validate import validate_payload

ROOT = Path(__file__).resolve().parents[2]
HORIZON_WEEKS = 13


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def seasonal_history(n: int = 120, period: int = 7, amp: float = 6.0,
                     noise: float = 1.0, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    cyc = np.array([amp * math.sin(2 * math.pi * i / period) for i in range(period)])
    return np.array([[cyc[t % period] + rng.normal(0, noise),
                      cyc[t % period] * 0.6 + rng.normal(0, noise)] for t in range(n)])


def flat_history(n: int = 120, noise: float = 2.0, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.array([[rng.normal(0, noise), rng.normal(0, noise)] for _ in range(n)])


def build_payload(history: np.ndarray, origins: int = 60, seed: int = 5) -> dict:
    rng = np.random.default_rng(seed)
    n = len(history)
    act = np.array([[100.0 + rng.normal(0, 2.0), 95.0 + rng.normal(0, 1.5)] for _ in range(n)])
    panel = []
    for o in range(min(origins, n - HORIZON_WEEKS)):
        fc, ac = [], []
        for k in range(HORIZON_WEEKS):
            t = o + k
            fc.append([float(act[t][0] - history[t][0]), float(act[t][1] - history[t][1])])
            ac.append([float(act[t][0]), float(act[t][1])])
        panel.append({'origin': 'W%03d' % o, 'forecast': fc, 'actual': ac})
    return {
        'unit': '万元',
        'quality_checks': {'reconciled': True, 'unique_records': True, 'complete': True,
                           'fresh': True, 'unit_consistent': True},
        'historical_forecast_errors': history.tolist(),
        'current_plan': {
            'weekly_inflow': [100.0] * HORIZON_WEEKS,
            'weekly_outflow': [100.0] * HORIZON_WEEKS,
            'cash_total': 1000.0, 'cash_restricted': 100.0,
            'committed_credit_available_now': 100.0, 'minimum_buffer': 100.0,
        },
        'panel': panel,
    }


# --------------------------------------------------------------------------
# core
# --------------------------------------------------------------------------

class TestSeasonality(unittest.TestCase):
    def test_detects_dominant_period(self):
        found = detect_seasonality(seasonal_history(period=7))
        self.assertEqual(found[0].period, 7)
        self.assertEqual(found[1].period, 7)
        self.assertGreater(found[0].strength, 0.5)

    def test_reports_none_when_no_cycle(self):
        found = detect_seasonality(flat_history())
        for s in found:
            self.assertIsNone(s.period)
            self.assertIn('non-seasonal', s.note)

    def test_insufficient_data_is_flagged(self):
        found = detect_seasonality(np.zeros((2, 2)))
        self.assertIsNone(found[0].period)

    def test_rejects_bad_shape(self):
        with self.assertRaises(ValueError):
            detect_seasonality(np.zeros(10))


class TestBlockSelection(unittest.TestCase):
    def test_aligns_to_detected_period(self):
        seas = detect_seasonality(seasonal_history(period=7))
        block, why = select_block_length(seas, 4)
        self.assertEqual(block, 7)
        self.assertIn('aligned', why)

    def test_falls_back_when_no_cycle(self):
        seas = detect_seasonality(flat_history())
        block, why = select_block_length(seas, 4)
        self.assertEqual(block, 4)
        self.assertIn('no seasonal cycle', why)

    def test_block_never_exceeds_horizon(self):
        class Fake:
            period = 40
        block, _ = select_block_length([Fake(), Fake()], 4, horizon=13)
        self.assertEqual(block, 13)

    def test_longer_period_wins_on_disagreement(self):
        class A:
            period = 5

        class B:
            period = 9
        block, _ = select_block_length([A(), B()], 4)
        self.assertEqual(block, 9)


class TestRollingState(unittest.TestCase):
    def test_incremental_matches_batch(self):
        data = seasonal_history(150)
        state = RollingState()
        state.update(data)
        np.testing.assert_allclose(state.mean, data.mean(axis=0), rtol=1e-12)
        np.testing.assert_allclose(state.std, data.std(axis=0), rtol=1e-12)

    def test_autocorrelation_matches_numpy(self):
        data = seasonal_history(150)
        state = RollingState()
        state.update(data)
        for lag in (1, 7, 14):
            expected = np.corrcoef(data[:-lag, 0], data[lag:, 0])[0, 1]
            self.assertAlmostEqual(state.autocorrelation(lag)[0], expected, places=9)

    def test_sliding_window_keeps_statistics_consistent(self):
        data = seasonal_history(150)
        state = RollingState()
        state.update(data)
        state.set_window(52)
        self.assertEqual(len(state.errors()), 52)
        # n still counts everything ever seen
        self.assertEqual(state.n, 150)
        window = state.errors()
        expected = np.corrcoef(window[:-7, 0], window[7:, 0])[0, 1]
        self.assertAlmostEqual(state.autocorrelation(7)[0], expected, places=9)

    def test_window_cap_minimum(self):
        state = RollingState()
        with self.assertRaises(ValueError):
            state.set_window(10)

    def test_empty_state_is_safe(self):
        state = RollingState()
        np.testing.assert_array_equal(state.mean, np.zeros(2))
        np.testing.assert_array_equal(state.std, np.zeros(2))
        with self.assertRaises(ValueError):
            state.errors()


class TestBiasResolution(unittest.TestCase):
    def test_structural_bias_is_kept(self):
        rng = np.random.default_rng(1)
        data = np.column_stack([rng.normal(1.5, 2.0, 300), rng.normal(1.0, 1.5, 300)])
        removed, info = resolve_bias(data, 'auto')
        self.assertEqual(info['mode'], 'auto')
        self.assertTrue(all(info['structural']))
        np.testing.assert_allclose(removed, np.zeros(2))

    def test_noise_mean_is_removed(self):
        rng = np.random.default_rng(2)
        data = np.column_stack([rng.normal(0.0, 2.0, 300), rng.normal(0.0, 1.5, 300)])
        removed, info = resolve_bias(data, 'auto')
        self.assertFalse(any(info['structural']))
        np.testing.assert_allclose(removed, data.mean(axis=0))

    def test_forced_modes(self):
        data = flat_history(60)
        always, _ = resolve_bias(data, True)
        never, _ = resolve_bias(data, False)
        np.testing.assert_allclose(always, data.mean(axis=0))
        np.testing.assert_allclose(never, np.zeros(2))

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            resolve_bias(flat_history(30), 'sometimes')

    def test_p_values_in_unit_interval(self):
        _, info = resolve_bias(flat_history(80), 'auto')
        for p in info['p_values']:
            self.assertTrue(0.0 <= p <= 1.0)


class TestSimulation(unittest.TestCase):
    def setUp(self):
        self.hist = seasonal_history(120)

    def test_reproducible_from_seed(self):
        a = simulate_paths([100.0] * 13, [100.0] * 13, self.hist, initial_capacity=1000.0,
                           draws=2000, seed=42)['paths']
        b = simulate_paths([100.0] * 13, [100.0] * 13, self.hist, initial_capacity=1000.0,
                           draws=2000, seed=42)['paths']
        np.testing.assert_array_equal(a, b)

    def test_different_seed_changes_paths(self):
        a = simulate_paths([100.0] * 13, [100.0] * 13, self.hist, initial_capacity=1000.0,
                           draws=2000, seed=1)['paths']
        b = simulate_paths([100.0] * 13, [100.0] * 13, self.hist, initial_capacity=1000.0,
                           draws=2000, seed=2)['paths']
        self.assertFalse(np.array_equal(a, b))

    def test_paths_start_at_capacity(self):
        paths = simulate_paths([100.0] * 13, [100.0] * 13, self.hist,
                               initial_capacity=1000.0, draws=1000)['paths']
        self.assertEqual(paths.shape, (1000, 14))
        np.testing.assert_allclose(paths[:, 0], 1000.0)

    def test_rejects_short_history(self):
        with self.assertRaises(ValueError):
            simulate_paths([100.0] * 13, [100.0] * 13, flat_history(10),
                           initial_capacity=0.0)

    def test_rejects_negative_plan(self):
        with self.assertRaises(ValueError):
            simulate_paths([-1.0] * 13, [100.0] * 13, self.hist, initial_capacity=0.0)

    def test_rejects_draws_out_of_range(self):
        with self.assertRaises(ValueError):
            simulate_paths([100.0] * 13, [100.0] * 13, self.hist,
                           initial_capacity=0.0, draws=10)

    def test_summary_statistics_are_finite(self):
        paths = simulate_paths([100.0] * 13, [100.0] * 13, self.hist,
                               initial_capacity=1000.0, draws=2000)['paths']
        stats = summarise_paths(paths, 100.0)
        for key, value in stats.items():
            self.assertTrue(math.isfinite(value), f'{key} is not finite')
        self.assertTrue(0.0 <= stats['probability_shortfall_scenario'] <= 1.0)


class TestWalkForward(unittest.TestCase):
    def setUp(self):
        self.hist = seasonal_history(170)
        self.payload = build_payload(self.hist, origins=140)

    def test_backtest_is_out_of_sample(self):
        result = walk_forward_backtest(
            self.hist, self.payload['panel'], warmup=26, draws=2000, seed=42)
        self.assertEqual(result.origins, len(self.payload['panel']) - 26)
        self.assertTrue(all(math.isfinite(x) for x in result.mae))
        self.assertGreater(result.origins, 0)

    def test_decisions_are_recorded(self):
        result = walk_forward_backtest(
            self.hist, self.payload['panel'], warmup=26, draws=1000, seed=42)
        for d in result.decisions:
            self.assertIn('block', d)
            self.assertIn('rationale', d)
            self.assertIn('bias', d)
            self.assertGreater(len(d['rationale']), 0)

    def test_rejects_warmup_below_minimum(self):
        with self.assertRaises(ValueError):
            walk_forward_backtest(self.hist, self.payload['panel'], warmup=5)

    def test_rejects_panel_shorter_than_warmup(self):
        with self.assertRaises(ValueError):
            walk_forward_backtest(self.hist, self.payload['panel'], warmup=999)

    def test_reproducible(self):
        a = walk_forward_backtest(self.hist, self.payload['panel'], warmup=26,
                                  draws=1000, seed=42).summary()
        b = walk_forward_backtest(self.hist, self.payload['panel'], warmup=26,
                                  draws=1000, seed=42).summary()
        self.assertEqual(a, b)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertTrue(Config().fingerprint())

    def test_rejects_bad_draws(self):
        with self.assertRaises(ValueError):
            Config(draws=10)

    def test_env_override(self):
        import os
        os.environ['CF_ROLLCAST_DRAWS'] = '2000'
        try:
            cfg = load_config()
            self.assertEqual(cfg.draws, 2000)
        finally:
            del os.environ['CF_ROLLCAST_DRAWS']

    def test_explicit_overrides_env(self):
        import os
        os.environ['CF_ROLLCAST_DRAWS'] = '2000'
        try:
            cfg = load_config(draws=3000)
            self.assertEqual(cfg.draws, 3000)
        finally:
            del os.environ['CF_ROLLCAST_DRAWS']

    def test_debias_parsing(self):
        self.assertEqual(load_config(debias='true').debias, True)
        self.assertEqual(load_config(debias='false').debias, False)
        self.assertEqual(load_config(debias='auto').debias, 'auto')

    def test_fingerprint_changes_with_config(self):
        self.assertNotEqual(Config(draws=1000).fingerprint(), Config(draws=2000).fingerprint())


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

class TestValidation(unittest.TestCase):
    def setUp(self):
        self.payload = build_payload(seasonal_history(120), origins=60)

    def test_valid_payload_passes(self):
        report = validate_payload(self.payload)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(len(report.input_sha256), 64)

    def test_quality_gate_blocks(self):
        payload = copy.deepcopy(self.payload)
        payload['quality_checks']['reconciled'] = False
        report = validate_payload(payload)
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0]['code'], 'GATE_FAILED')

    def test_gate_must_be_boolean(self):
        payload = copy.deepcopy(self.payload)
        payload['quality_checks']['fresh'] = 'yes'
        report = validate_payload(payload)
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0]['code'], 'GATE_NOT_BOOL')

    def test_missing_unit(self):
        payload = copy.deepcopy(self.payload)
        del payload['unit']
        report = validate_payload(payload)
        self.assertEqual(report.errors[0]['code'], 'UNIT_MISSING')

    def test_short_history(self):
        payload = copy.deepcopy(self.payload)
        payload['historical_forecast_errors'] = [[0.0, 0.0]] * 10
        report = validate_payload(payload)
        self.assertEqual(report.errors[0]['code'], 'HISTORY_TOO_SHORT')

    def test_negative_inflow(self):
        payload = copy.deepcopy(self.payload)
        payload['current_plan']['weekly_inflow'][0] = -5.0
        report = validate_payload(payload)
        self.assertEqual(report.errors[0]['code'], 'PLAN_NEGATIVE')

    def test_restricted_exceeds_total(self):
        payload = copy.deepcopy(self.payload)
        payload['current_plan']['cash_restricted'] = 5000.0
        report = validate_payload(payload)
        self.assertEqual(report.errors[0]['code'], 'CASH_INCONSISTENT')

    def test_unsorted_panel(self):
        payload = copy.deepcopy(self.payload)
        payload['panel'][5]['origin'] = 'W00000'
        report = validate_payload(payload)
        self.assertEqual(report.errors[0]['code'], 'PANEL_UNSORTED')

    def test_nan_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload['current_plan']['weekly_inflow'][2] = None
        report = validate_payload(payload)
        self.assertEqual(report.errors[0]['code'], 'NOT_FINITE')

    def test_short_history_warns(self):
        report = validate_payload(self.payload)
        codes = [w['code'] for w in report.warnings]
        self.assertNotIn('SHORT_HISTORY', codes)  # 120 weeks is enough

    def test_warning_for_30_weeks(self):
        payload = build_payload(seasonal_history(30), origins=5)
        report = validate_payload(payload)
        codes = [w['code'] for w in report.warnings]
        self.assertIn('SHORT_HISTORY', codes)


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------

class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.payload = build_payload(seasonal_history(130), origins=70)
        self.cfg = load_config(draws=2000, seed=42, warmup=26)

    def test_full_run(self):
        result = run_pipeline(self.payload, self.cfg)
        self.assertEqual(result['status'], 'calculated')
        self.assertEqual(result['validation']['ok'], True)
        self.assertIn('forecast', result)
        self.assertIn('backtest', result)
        self.assertEqual(len(result['trace']), 4)

    def test_blocked_run_stops_before_forecast(self):
        payload = copy.deepcopy(self.payload)
        payload['quality_checks']['complete'] = False
        result = run_pipeline(payload, self.cfg)
        self.assertEqual(result['status'], 'blocked')
        self.assertNotIn('forecast', result)

    def test_run_without_panel_skips_backtest(self):
        payload = copy.deepcopy(self.payload)
        del payload['panel']
        result = run_pipeline(payload, self.cfg)
        self.assertEqual(result['backtest']['status'], 'not_run')
        self.assertEqual(result['status'], 'calculated')

    def test_output_is_json_serialisable(self):
        result = run_pipeline(self.payload, self.cfg)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertIsInstance(json.loads(encoded), dict)

    def test_reproducible_end_to_end(self):
        a = run_pipeline(self.payload, self.cfg, run_id='fixed-a')
        b = run_pipeline(self.payload, self.cfg, run_id='fixed-b')
        self.assertEqual(a['forecast']['mean_capacity_path'],
                         b['forecast']['mean_capacity_path'])
        self.assertEqual(a['forecast']['gap_p95'], b['forecast']['gap_p95'])

    def test_explicit_run_id_is_used(self):
        result = run_pipeline(self.payload, self.cfg, run_id='my-run')
        self.assertEqual(result['run_id'], 'my-run')

    def test_config_fingerprint_recorded(self):
        result = run_pipeline(self.payload, self.cfg)
        self.assertEqual(result['config_fingerprint'], self.cfg.fingerprint())

    def test_missing_plan_blocks_rather_than_raising(self):
        '''A malformed payload is a data problem, so it must come back as
        "blocked" with codes, not as an exception the caller has to catch.'''
        payload = copy.deepcopy(self.payload)
        del payload['current_plan']
        result = run_pipeline(payload, self.cfg)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['validation']['errors'][0]['code'], 'PLAN_MISSING')

    def test_retry_exhausts_then_raises_stage_error(self):
        from rollcast.pipeline import _retry

        calls = {'n': 0}

        def always_fails():
            calls['n'] += 1
            raise RuntimeError('boom')

        with self.assertRaises(StageError) as ctx:
            _retry(3, 0.0, 'persist', always_fails)
        self.assertEqual(calls['n'], 3)
        self.assertIn('persist', str(ctx.exception))

    def test_retry_returns_first_success(self):
        from rollcast.pipeline import _retry

        calls = {'n': 0}

        def fails_once():
            calls['n'] += 1
            if calls['n'] < 2:
                raise RuntimeError('transient')
            return 'ok'

        self.assertEqual(_retry(3, 0.0, 'persist', fails_once), 'ok')
        self.assertEqual(calls['n'], 2)


class TestStoreHelpers(unittest.TestCase):
    '''These need no database: they cover error classification, which decides
    whether a failure is retried or surfaced immediately.'''

    def test_errno_is_read_from_args(self):
        from rollcast.store import _errno
        # pymysql raises IntegrityError(1062, "...") with no errno attribute.
        self.assertEqual(_errno(Exception(1062, 'Duplicate entry')), 1062)
        self.assertIsNone(_errno(RuntimeError('no number here')))

    def test_permanent_errors_are_not_retried(self):
        from rollcast.store import _classify
        self.assertTrue(_classify(Exception(1146, 'table missing')).permanent)
        self.assertTrue(_classify(Exception(1142, 'denied')).permanent)
        self.assertFalse(_classify(Exception(2003, 'cannot connect')).permanent)

    def test_missing_table_message_is_actionable(self):
        from rollcast.store import _classify
        message = str(_classify(Exception(1146, "doesn't exist")))
        self.assertIn('--init-db', message)

    def test_json_equal_tolerates_double_roundtrip(self):
        from rollcast.store import _json_equal
        # MySQL stores JSON numbers via DOUBLE.
        self.assertTrue(_json_equal({'a': 0.9127583004940999}, {'a': 0.9127583004941}))
        self.assertFalse(_json_equal({'a': 1.0}, {'a': 1.5}))
        # Structure must still match exactly.
        self.assertFalse(_json_equal({'a': 1}, {'a': 1, 'b': 2}))
        self.assertFalse(_json_equal({'a': [1, 2]}, {'a': [1, 2, 3]}))
        # Booleans must not be conflated with numbers.
        self.assertFalse(_json_equal({'a': True}, {'a': 1}))


class TestGoldenRegression(unittest.TestCase):
    '''Freeze the headline numbers so a refactor cannot drift silently.'''

    def test_seasonal_block_alignment_and_bias(self):
        hist = seasonal_history(120, period=7, amp=6.0, noise=1.0, seed=7)
        seas = detect_seasonality(hist)
        block, _ = select_block_length(seas, 4)
        self.assertEqual(block, 7)

        rng = np.random.default_rng(42)
        paths = simulate_paths([100.0] * 13, [100.0] * 13, hist,
                               initial_capacity=1000.0, draws=20000,
                               block=block, seed=42)['paths']
        terminal = paths[:, -1]
        # With the bias removed the mean terminal capacity must sit at the
        # planned level, not drift by horizon * mean_error.
        self.assertLess(abs(terminal.mean() - 1000.0), 1.0)
        self.assertGreater(terminal.std(), 0.0)

    def test_structural_bias_survives(self):
        rng = np.random.default_rng(9)
        biased = np.column_stack([rng.normal(2.0, 1.0, 400), rng.normal(1.0, 1.0, 400)])
        removed, info = resolve_bias(biased, 'auto')
        np.testing.assert_allclose(removed, np.zeros(2), atol=1e-12)
        self.assertTrue(all(info['structural']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
