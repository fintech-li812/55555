import copy
import unittest

import numpy as np

from risk_model import (benford_test, bh_adjust, calibrated_path_lower_bound,
                       cashflow_risk, first_digit, fit_monotone_margin_lp,
                       last_digit, legacy_fuse, legacy_scores)
from panel_train import train_panel


def good_input():
    return {'cash_total': 1000., 'cash_restricted': 100.,
            'committed_credit_available_now': 100., 'minimum_buffer': 100.,
            'unit': '万元', 'weekly_inflow': [100.] * 13,
            'weekly_outflow': [100.] * 13,
            'historical_forecast_errors': [[0., 0.]] * 52,
            'quality_checks': {k: True for k in
                ('reconciled', 'unique_records', 'complete', 'fresh', 'unit_consistent')},
            'simulations': 1000, 'seed': 42, 'model_validated': False}


class RiskTests(unittest.TestCase):
    def test_digit_boundaries_and_rounding(self):
        self.assertIsNone(first_digit(0))
        self.assertEqual(first_digit(-.0045), 4)
        self.assertEqual(first_digit(9.99999999999), 9)
        self.assertEqual(last_digit(.045), 5)
        self.assertEqual(last_digit(-1.005), 1)
        self.assertEqual(last_digit(1e30), 0)

    def test_missing_and_infinity_do_not_turn_green(self):
        for val in (None, float('nan'), float('inf'), True, '1'):
            d = good_input()
            d['weekly_inflow'][0] = val
            with self.subTest(val=val), self.assertRaises((ValueError, TypeError)):
                cashflow_risk(d)

    def test_benford_eligibility_and_small_sample(self):
        vals = list(range(1, 51))
        self.assertIsNone(benford_test(vals)['p_value'])
        r = benford_test(vals, eligible=True, simulations=999)
        self.assertLess(r['min_expected_count'], 5)
        self.assertIn('Monte Carlo', r['method'])
        self.assertGreater(r['p_value'], 0)

    def test_false_discovery_adjustment(self):
        np.testing.assert_allclose(bh_adjust([.01, .04, .03]), [.03, .04, .04])

    def test_legacy_fusion_all_boundaries_and_high_st_normal(self):
        self.assertEqual(legacy_fuse(.3, -1, .7)['light'], '红灯')
        self.assertEqual(legacy_fuse(.5, -1, .29)['light'], '黄灯')
        self.assertEqual(legacy_fuse(.3, 0, .3)['light'], '黄灯')
        self.assertEqual(legacy_fuse(.5, 0, 1)['light'], '绿灯')
        self.assertIn('不能排除', legacy_fuse(.8, -1, 0)['reason'])

    def test_legacy_demo_q_and_margin(self):
        x = [.045, .065243, .07, 1.45, 1.05, 3.200255, .020710,
             68.000625, .1, 4.322034, 3.500137, .832114, .04, .630003, .020803]
        r = legacy_scores(x)
        self.assertAlmostEqual(r['Q_legacy'], .7)
        self.assertAlmostEqual(r['D_legacy'], -.05549, places=5)

    def test_lp_direction_on_known_separable_data(self):
        fit = fit_monotone_margin_lp([[0.], [.1], [.9], [1.]], [1, 1, 0, 0])
        self.assertLess(.1, fit['threshold'])
        self.assertGreater(.9, fit['threshold'])
        self.assertAlmostEqual(fit['loss'], 0.)

    def test_quality_gate(self):
        d = good_input()
        d['quality_checks']['reconciled'] = False
        self.assertEqual(cashflow_risk(d)['status'], '待复核')
        del d['quality_checks']['complete']
        with self.assertRaises(ValueError):
            cashflow_risk(d)

    def test_unvalidated_model_never_green(self):
        self.assertEqual(cashflow_risk(good_input())['status'], '黄灯')

    def test_credit_counted_once_restricted_cash_excluded(self):
        d = good_input()
        d['weekly_inflow'] = [0.] * 13
        r = cashflow_risk(d)
        self.assertEqual(r['cash_available'], 900.)
        self.assertEqual(r['planned_capacity_path'][-1], -300.)
        self.assertEqual(r['baseline_first_breach_week'], 10)
        self.assertEqual(r['status'], '红灯')

    def test_initial_shortfall_and_equality(self):
        d = good_input()
        d.update(cash_total=100., cash_restricted=0., committed_credit_available_now=0.)
        self.assertIsNone(cashflow_risk(d)['baseline_first_breach_week'])
        d['cash_total'] = 99.
        self.assertEqual(cashflow_risk(d)['baseline_first_breach_week'], 0)

    def test_paired_errors_preserve_cancellation(self):
        d = good_input()
        d['historical_forecast_errors'] = [[50., 50.], [-50., -50.]] * 26
        r = cashflow_risk(d)
        self.assertEqual(r['probability_shortfall_scenario'], 0.)
        self.assertEqual(r['gap_cvar95'], 0.)

    def test_stress_delay_and_receipts_beyond_horizon(self):
        d = good_input()
        d['stress'] = {'inflow_haircut': 0, 'outflow_uplift': 0, 'receipt_delay_weeks': 2}
        r = cashflow_risk(d)
        self.assertEqual(r['stress_receipts_beyond_horizon'], 200.)
        self.assertEqual(r['stress_capacity_path'][1], 900.)
        self.assertEqual(r['stress_capacity_path'][-1], 800.)

    def test_simulation_probability_and_tail_loss(self):
        d = good_input()
        d.update(cash_total=200., cash_restricted=0., committed_credit_available_now=0.)
        d['historical_forecast_errors'] = [[-50., 50.]] * 52
        r = cashflow_risk(d)
        self.assertEqual(r['probability_shortfall_scenario'], 1.)
        self.assertEqual(r['gap_cvar95'], 1200.)

    def test_pathwise_calibration_not_pointwise(self):
        pred = np.ones((19, 13)) * 100
        actual = pred.copy()
        actual[:, 3] -= np.arange(1, 20)
        self.assertEqual(calibrated_path_lower_bound(pred, actual, .1), 18.)
        with self.assertRaises(ValueError):
            calibrated_path_lower_bound(pred[:2], actual[:2], .01)

    def test_same_seed_same_results(self):
        d = good_input()
        d['historical_forecast_errors'] = [[i, -i] for i in range(-26, 26)]
        self.assertEqual(cashflow_risk(d), cashflow_risk(copy.deepcopy(d)))

    def test_calibration_can_escalate_validated_green(self):
        d = good_input()
        d.update(model_validated=True, stress={'inflow_haircut': 0, 'outflow_uplift': 0, 'receipt_delay_weeks': 0})
        self.assertEqual(cashflow_risk(d)['status'], '绿灯')
        d['path_calibration'] = {'predicted': [[1000.] * 13] * 19,
                                 'actual': [[0.] * 13] * 19, 'alpha': .1}
        self.assertEqual(cashflow_risk(d)['status'], '黄灯')

    def test_panel_future_information_rejected(self):
        row = {'company_id': 'a', 'as_of': '2021-01-01', 'available_at': '2021-02-01',
               'label_end': '2021-04-01', 'label': 1, 'features': {'X1': 0}}
        with self.assertRaisesRegex(ValueError, 'look-ahead'):
            train_panel([row], ['X1'], '2022-01-01', '2023-01-01', target='future_ST')

    def test_panel_training_separate_dates_companies_and_test(self):
        rows = []
        for year in (2020, 2022, 2024):
            for i in range(20):
                rows.append({'company_id': f'{year}-{i}', 'as_of': f'{year}-01-01',
                             'available_at': f'{year}-01-01', 'label_end': f'{year}-04-01',
                             'label': i % 2, 'features': {'health': 1 - i % 2 + i / 1000}})
        _, r = train_panel(rows, ['health'], '2021-01-01', '2023-01-01', target='future_ST')
        self.assertEqual(r['counts_train_calibration_test'], [20, 20, 20])
        self.assertEqual(r['roc_auc'], 1.)  # synthetic separable unit case, not business accuracy
        self.assertIn('not population probability', r['probability_interpretation'])
        rows[-1]['company_id'] = rows[0]['company_id']
        with self.assertRaisesRegex(ValueError, 'company overlap'):
            train_panel(rows, ['health'], '2021-01-01', '2023-01-01', target='future_ST')


if __name__ == '__main__':
    unittest.main(verbosity=2)
