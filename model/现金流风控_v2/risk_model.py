'''Cash-flow risk reference implementation v2; probabilities are scenario estimates.

No network calls, no notifications, no financial transactions. Money uses one
explicit unit throughout. Historical errors must be actual minus as-of forecast.
'''
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import linprog
from scipy.stats import chi2


VERSION = '2.0.0'
HB = [1, 4, 1, 1, 1, 2, 2, 2, 1, 3, 3, 1, 4, 5, 3]
HM = [2, 1, 2, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1]
LEGACY = [
    (7, 15, 180, 0.0), (0, .02, .20, .183087),
    (2, .02, .25, .191136), (6, -.30, .80, .190756),
    (8, -.10, .45, .051099), (12, .01, .18, .158145),
    (14, .01, .15, .225783),
]


def number(value, name='value'):
    if isinstance(value, (bool, str)) or value is None:
        raise ValueError(f'{name}: requires a finite number')
    val = float(value)
    if not math.isfinite(val):
        raise ValueError(f'{name}: requires a finite number')
    return val


def array(values, name, ndim=1):
    raw = np.asarray(values, dtype=object)
    if raw.ndim != ndim or raw.size == 0:
        raise ValueError(f'{name}: nonempty {ndim}D array required')
    vals = np.array([number(v, name) for v in raw.flat]).reshape(raw.shape)
    return vals


def first_digit(value):
    # Decimal avoids rounding 9.9999999999 into a first digit of 1.
    val = Decimal(str(number(value))).copy_abs()
    return None if val.is_zero() else int(val.as_tuple().digits[0])


def last_digit(value):
    val = Decimal(str(number(value))).copy_abs()
    # Financial values of arbitrary exponent need sufficient decimal precision.
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = max(32, len(val.as_tuple().digits) + abs(val.adjusted()) + 4)
        rounded = val.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        return int(rounded * 100) % 10


def legacy_scores(x15):
    x = array(x15, 'x15')
    if len(x) != 15:
        raise ValueError('x15: exactly 15 ratios required')
    b = [int(first_digit(v) == h) for v, h in zip(x, HB)]
    m = [int(last_digit(v) == h) for v, h in zip(x, HM)]
    # Keep published rounded coefficients unchanged for backward comparison.
    score = sum(w * (.1 + np.clip((x[i] - lo) / (hi - lo), -1, 1))
                for i, lo, hi, w in LEGACY)
    return {'Q_legacy': (30 - sum(b) - sum(m)) / 30,
            'score_legacy': float(score), 'D_legacy': float(2 * (score - .314253)),
            'B': b, 'M': m, 'meaning': 'legacy screening; not fraud evidence'}


def legacy_fuse(q, d, vote):
    """v2 遗留融合规则，仅用于与旧口径做向后对比，**不是 v8 的权威口径**。

    v8 的权威融合规则见 ``mcp-server/program_v1/src/v8/fusion.py``。
    两者在绝大多数输入上结论一致，唯一的已知差异是
    ``d >= 0 且 vote >= 0.7``：legacy 判绿灯，v8 判黄灯（FUSE-R6，
    理由：财务健康结论与舞弊证据冲突时不应放行绿灯）。
    该差异由 ``tests/test_fusion_rules.py`` 显式固定，防止两套规则静默漂移。

    本函数还会在 q/vote 越界或缺失时抛 ValueError——这是 legacy 的输入校验，
    v8 相应场景改为返回「红灯/黄灯 + 不可用原因」，不抛异常。
    """
    q, d, vote = number(q), number(d), number(vote)
    if not 0 <= q <= 1 or not 0 <= vote <= 1:
        raise ValueError('q and vote must be in [0,1]')
    # Separate stable status codes from human-readable reasons.
    if q < .3:
        return {'light': '红灯', 'reason': '旧Q低于0.3，待核查；不代表舞弊'}
    if d < 0 and vote >= .7:
        return {'light': '红灯', 'reason': '旧财务判别与ST投票共同预警'}
    if d < 0:
        return {'light': '黄灯', 'reason': '旧财务判别预警，不能排除舞弊'}
    if q < .5:
        return {'light': '黄灯', 'reason': '旧Q存疑，需复核'}
    return {'light': '绿灯', 'reason': '仅旧模型规则通过'}


def benford_test(amounts, *, eligible=False, simulations=10000, seed=42):
    vals = array(amounts, 'amounts')
    digits = [first_digit(v) for v in vals if v != 0]
    if not digits:
        return {'status': '不适用', 'n': 0, 'p_value': None}
    counts = np.bincount(digits, minlength=10)[1:]
    n = len(digits)
    p = np.log10(1 + 1 / np.arange(1, 10))
    stat = float(np.sum((counts - n * p) ** 2 / (n * p)))
    result = {'n': n, 'chi_square': stat, 'min_expected_count': float(n * p.min()),
              'status': '不适用', 'p_value': None,
              'meaning': 'distribution clue, not proof of quality or fraud'}
    if not eligible:
        return result
    # Eligibility is a caller-documented population decision, not inferred from p.
    if n * p.min() >= 5:
        pv, method = float(chi2.sf(stat, 8)), 'chi-square df=8'
    else:
        if isinstance(simulations, bool) or not isinstance(simulations, int) or simulations < 999:
            raise ValueError('simulations must be integer >=999')
        draws = np.random.default_rng(seed).multinomial(n, p, size=simulations)
        stats = np.sum((draws - n * p) ** 2 / (n * p), axis=1)
        pv = float((1 + np.count_nonzero(stats >= stat)) / (simulations + 1))
        method = 'multinomial Monte Carlo under Benford null'
    result.update(status='已检验', p_value=pv, method=method)
    return result


def bh_adjust(p_values):
    p = array(p_values, 'p_values')
    if np.any((p < 0) | (p > 1)):
        raise ValueError('p-values outside [0,1]')
    order = np.argsort(p)
    adj = np.minimum.accumulate((p[order] * len(p) / np.arange(1, len(p) + 1))[::-1])[::-1]
    out = np.empty_like(p)
    out[order] = np.minimum(adj, 1)
    return out.tolist()


def fit_monotone_margin_lp(health_features, labels, margin=.05):
    '''A signed linear margin model, deliberately not called DEA-DA.

    Each column must already mean higher=healthier and be scaled in training.
    labels: ST=1, healthy=0. Only call inside the training split.
    '''
    x = array(health_features, 'health_features', 2)
    y = array(labels, 'labels')
    n, p = x.shape
    if len(y) != n or set(y.tolist()) != {0., 1.}:
        raise ValueError('aligned labels containing both 0 and 1 required')
    margin = number(margin)
    if margin <= 0:
        raise ValueError('positive margin required')
    # z=+1 healthy, -1 ST; z*(xw-c) >= margin-slack.
    z = 1 - 2 * y
    a = np.zeros((n, p + 1 + n))
    a[:, :p] = -z[:, None] * x
    a[:, p] = z
    a[np.arange(n), p + 1 + np.arange(n)] = -1
    objective = np.zeros(p + 1 + n)
    for label in (0, 1):
        mask = y == label
        objective[p + 1 + np.flatnonzero(mask)] = .5 / mask.sum()
    eq = np.zeros((1, p + 1 + n))
    eq[0, :p] = 1
    solved = linprog(objective, A_ub=a, b_ub=np.full(n, -margin),
                     A_eq=eq, b_eq=[1],
                     bounds=[(0, None)] * p + [(None, None)] + [(0, None)] * n,
                     method='highs')
    if not solved.success:
        raise RuntimeError(solved.message)
    return {'weights': solved.x[:p].tolist(), 'threshold': float(solved.x[p]),
            'loss': float(solved.fun), 'type': 'monotone linear margin LP, not DEA'}


def calibrated_path_lower_bound(predicted_paths, realized_paths, alpha=.1):
    '''One-sided simultaneous lower envelope over a whole future cash path.

    Use held-out, fully matured forecast origins. Exchangeability is required
    for the textbook finite-sample guarantee; temporal use is empirical only.
    '''
    pred = array(predicted_paths, 'predicted_paths', 2)
    actual = array(realized_paths, 'realized_paths', 2)
    if pred.shape != actual.shape or not 0 < alpha < 1:
        raise ValueError('matching paths and alpha in (0,1) required')
    scores = np.maximum(0, np.max(pred - actual, axis=1))
    rank = math.ceil((len(scores) + 1) * (1 - alpha))
    if rank > len(scores):
        raise ValueError('insufficient calibration origins for finite bound')
    return float(np.partition(scores, rank - 1)[rank - 1])


def _integer(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f'{name}: integer in [{lo},{hi}] required')
    return value


def cashflow_risk(data):
    '''13-week scheduled-flow engine; input format documented in README.'''
    inflow = array(data['weekly_inflow'], 'weekly_inflow')
    outflow = array(data['weekly_outflow'], 'weekly_outflow')
    if len(inflow) != 13 or len(outflow) != 13 or np.any(inflow < 0) or np.any(outflow < 0):
        raise ValueError('13 nonnegative weekly gross inflows and outflows required')
    total = number(data['cash_total'], 'cash_total')
    restricted = number(data['cash_restricted'], 'cash_restricted')
    credit = number(data['committed_credit_available_now'], 'committed_credit_available_now')
    buffer = number(data['minimum_buffer'], 'minimum_buffer')
    if min(total, restricted, credit, buffer) < 0 or restricted > total:
        raise ValueError('invalid cash, restricted cash, committed credit or buffer')
    if not isinstance(data['unit'], str) or not data['unit'].strip():
        raise ValueError('explicit monetary unit required')
    checks = data['quality_checks']
    required = ('reconciled', 'unique_records', 'complete', 'fresh', 'unit_consistent')
    if any(type(checks.get(k)) is not bool for k in required):
        raise ValueError('five explicit boolean quality_checks required')
    failed = [k for k in required if not checks[k]]
    base = {'version': VERSION, 'unit': data['unit'], 'quality_failed': failed,
            'external_action_executed': False}
    if failed:
        return dict(base, status='待复核', reason='data gate blocked; repair and rerun')
    history = array(data['historical_forecast_errors'], 'historical_forecast_errors', 2)
    if history.shape[1] != 2 or len(history) < 26:
        raise ValueError('>=26 chronological paired actual-minus-forecast errors required')
    block = _integer(data.get('block_length', 4), 'block_length', 1, len(history))
    draws = _integer(data.get('simulations', 5000), 'simulations', 1000, 100000)
    seed = _integer(data.get('seed', 42), 'seed', 0, 2**32 - 1)
    # Circular moving blocks preserve local time and inflow/outflow dependence.
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(history), size=(draws, math.ceil(13 / block)))
    indices = ((starts[:, :, None] + np.arange(block)) % len(history)).reshape(draws, -1)[:, :13]
    errors = history[indices]
    receipts = np.maximum(0, inflow + errors[:, :, 0])
    payments = np.maximum(0, outflow + errors[:, :, 1])
    available = total - restricted
    initial_capacity = available + credit
    paths = np.column_stack([np.full(draws, initial_capacity),
                             initial_capacity + np.cumsum(receipts - payments, axis=1)])
    # Credit is total immediately drawable, unencumbered incremental capacity;
    # it is added ONCE, never once per week. Fees already belong in outflows.
    minimum = paths.min(axis=1)
    gap = np.maximum(0, buffer - minimum)
    p_short = float(np.mean(gap > 0))
    k = max(1, math.ceil(.05 * draws))
    worst = np.sort(gap)[-k:]
    planned_cash = available + np.r_[0, np.cumsum(inflow - outflow)]
    planned_capacity = planned_cash + credit
    breaching = np.flatnonzero(planned_capacity < buffer)
    stress = data.get('stress', {})
    haircut = number(stress.get('inflow_haircut', .2))
    uplift = number(stress.get('outflow_uplift', .1))
    delay = _integer(stress.get('receipt_delay_weeks', 2), 'receipt_delay_weeks', 0, 13)
    if not 0 <= haircut <= 1 or uplift < 0:
        raise ValueError('invalid stress haircut/uplift')
    stressed_in = inflow * (1 - haircut)
    delayed = np.zeros(13)
    if delay < 13:
        delayed[delay:] = stressed_in[:13-delay]
    spill = float(stressed_in[13-delay:].sum()) if delay else 0.
    stress_path = initial_capacity + np.r_[0, np.cumsum(delayed - outflow * (1 + uplift))]
    validated = data.get('model_validated', False)
    if type(validated) is not bool:
        raise ValueError('model_validated must be boolean')
    red = number(data.get('red_probability', .30))
    yellow = number(data.get('yellow_probability', .10))
    if not 0 <= yellow < red <= 1:
        raise ValueError('require 0<=yellow_probability<red_probability<=1')
    reasons = []
    if planned_capacity.min() < buffer:
        light = '红灯'
        reasons.append('baseline capacity breaches minimum buffer')
    elif p_short >= red:
        light = '红灯'
        reasons.append('scenario shortfall fraction reaches provisional red threshold')
    elif p_short >= yellow or stress_path.min() < buffer or not validated:
        light = '黄灯'
        reasons.append('scenario/stress warning or no real-data validation')
    else:
        light = '绿灯'
        reasons.append('validated scope and configured rules pass')
    path_bound = None
    if 'path_calibration' in data:
        cal = data['path_calibration']
        if np.asarray(cal['predicted']).ndim != 2 or np.asarray(cal['predicted']).shape[1] != 13:
            raise ValueError('path calibration must have 13 future weeks per origin')
        q = calibrated_path_lower_bound(cal['predicted'], cal['actual'], cal.get('alpha', .1))
        lower = planned_capacity[1:] - q
        path_bound = {'correction': q, 'capacity_lower': lower.tolist(),
                      'interpretation': 'empirical for time series; not guaranteed coverage'}
        if np.min(lower) < buffer and light == '绿灯':
            light = '黄灯'
            reasons.append('calibrated lower path breaches buffer')
    return dict(base, status=light, reason=reasons,
                model_validation_status='user-declared validated' if validated else 'not validated',
                cash_available=available, initial_total_capacity=initial_capacity,
                planned_cash_path=planned_cash.tolist(), planned_capacity_path=planned_capacity.tolist(),
                baseline_first_breach_week=int(breaching[0]) if breaching.size else None,
                probability_shortfall_scenario=p_short,
                monte_carlo_standard_error=float(math.sqrt(p_short * (1-p_short) / draws)),
                gap_p95=float(np.quantile(gap, .95, method='higher')),
                gap_cvar95=float(worst.mean()), mean_gap=float(gap.mean()),
                median_gap=float(np.median(gap)),
                stress_capacity_path=stress_path.tolist(),
                stress_gap=float(max(0, buffer - stress_path.min())),
                stress_receipts_beyond_horizon=spill,
                path_calibration=path_bound, seed=seed, simulations=draws,
                historical_error_weeks=len(history),
                warning='Scenario frequencies are not calibrated event probabilities; weekly netting misses intrawEEK shortfalls.')


def main():
    parser = argparse.ArgumentParser(description='13-week cash-flow risk v2')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8-sig'))
        result = cashflow_risk(data)
        result['input_sha256'] = hashlib.sha256(raw).hexdigest()
    except (ValueError, KeyError, TypeError) as exc:
        result = {'version': VERSION, 'status': '待复核', 'input_error': str(exc),
                  'external_action_executed': False}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        raise SystemExit(2)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(f'{result["status"]} -> {args.output}')


if __name__ == '__main__':
    main()
