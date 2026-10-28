'''Rolling walk-forward cash-flow forecasting core.

This extends the v2 13-week Monte Carlo engine (model/现金流风控_v2/risk_model.py)
from a single one-shot simulation into a true rolling forecast:

  * multiple forecast origins are evaluated out-of-sample (walk-forward),
  * the sampling block length is aligned to the detected seasonality,
  * a systematic bias term is estimated and removed so it no longer
    integrates into the 13-week cumulative path,
  * the error statistics are maintained incrementally so a new origin
    costs O(1) instead of a full rescan.

Money is float64 inside the Monte Carlo (it is a simulation, not bookkeeping)
but every money-like figure returned to callers is rounded to 2 decimals, and
every random draw is reproducible from an explicit seed.
'''
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

HORIZON = 13
MONEY_DP = 2


def _round_money(value: float) -> float:
    '''Round to 2 decimals with half-away-from-zero, matching the v2 engine.'''
    if value is None or not math.isfinite(value):
        return float(value)
    return float(np.round(float(value) + 0.0, MONEY_DP))


def _betacf(a: float, b: float, x: float) -> float:
    '''Continued fraction for the incomplete beta function (Lentz's method).'''
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    '''Regularised incomplete beta I_x(a, b). Kept local to avoid a scipy dependency.'''
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta + b * math.log1p(-x) + a * math.log(x)) * _betacf(b, a, 1.0 - x) / b


def _t_two_sided_p(t_stat: float, df: int) -> float:
    if df <= 0 or not math.isfinite(t_stat):
        return float('nan')
    return _betainc(df / 2.0, 0.5, df / (df + t_stat * t_stat))


def _mean_error_is_structural(errors: np.ndarray, alpha: float = 0.05) -> tuple[list[bool], list[float]]:
    '''Test each error column's mean against zero.

    A mean that survives the test is a persistent bias of the forecasting
    process: it will be there next quarter too, so removing it would bias the
    forecast in the opposite direction. A mean that does not survive is
    indistinguishable from sampling noise, and carrying it into a 13-week
    cumulative path only adds drift.

    Returns (is_structural, p_values) per column.
    '''
    err = _as_2d(errors, 'errors', cols=2)
    n = err.shape[0]
    if n < 3:
        return [False, False], [float('nan')] * 2
    mean = err.mean(axis=0)
    std = err.std(axis=0, ddof=1)
    structural: list[bool] = []
    p_values: list[float] = []
    for j in range(2):
        if std[j] == 0:
            structural.append(False)
            p_values.append(0.0 if mean[j] != 0 else 1.0)
            continue
        t_stat = mean[j] / (std[j] / math.sqrt(n))
        p = _t_two_sided_p(float(t_stat), n - 1)
        p_values.append(p)
        structural.append(bool(p < alpha))
    return structural, p_values


def _as_2d(values: Any, name: str, rows: int | None = None, cols: int | None = None) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 2 or arr.size == 0:
        raise ValueError(f'{name}: nonempty 2D array required')
    if not np.all(np.isfinite(arr)):
        raise ValueError(f'{name}: contains NaN or infinite values')
    if rows is not None and arr.shape[0] != rows:
        raise ValueError(f'{name}: expected {rows} rows, got {arr.shape[0]}')
    if cols is not None and arr.shape[1] != cols:
        raise ValueError(f'{name}: expected {cols} columns, got {arr.shape[1]}')
    return arr


# --------------------------------------------------------------------------
# Seasonality detection and block-length selection
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Seasonality:
    period: int | None          # detected period in weeks, None when inconclusive
    strength: float             # autocorrelation at that period
    method: str                 # how the decision was reached
    note: str                   # human-readable rationale, never silent


def detect_seasonality(
    errors: np.ndarray,
    max_period: int = 26,
    min_strength: float = 0.30,
) -> list[Seasonality]:
    '''Detect the dominant cycle of each error column via autocorrelation.

    A column is only declared seasonal when the peak autocorrelation reaches
    `min_strength`. Anything weaker returns period=None so the caller can fall
    back to a configured block length *and record that it did so*.
    '''
    err = _as_2d(errors, 'errors', cols=2)
    n = err.shape[0]
    out: list[Seasonality] = []
    for j, label in enumerate(('receipts', 'payments')):
        series = err[:, j]
        if series.std() == 0 or n < 3 * 2:
            out.append(Seasonality(None, 0.0, 'insufficient data',
                                   f'{label}: too few observations to detect a cycle'))
            continue
        centred = series - series.mean()
        denom = float(np.dot(centred, centred))
        best_lag, best_corr = 1, 0.0
        upper = min(max_period, n // 2)
        for lag in range(1, upper + 1):
            corr = float(np.dot(centred[:-lag], centred[lag:]) / denom)
            if abs(corr) > abs(best_corr):
                best_lag, best_corr = lag, corr
        if abs(best_corr) >= min_strength:
            out.append(Seasonality(
                best_lag, round(best_corr, 4), 'autocorrelation peak',
                f'{label}: {best_lag}-week cycle at r={best_corr:+.3f}'))
        else:
            out.append(Seasonality(
                None, round(best_corr, 4), 'autocorrelation peak',
                f'{label}: strongest cycle r={best_corr:+.3f} below {min_strength}, '
                'treated as non-seasonal'))
    return out


def select_block_length(
    seasonality: list[Seasonality],
    configured: int,
    horizon: int = HORIZON,
) -> tuple[int, str]:
    '''Pick the moving-block length, preferring the detected period.

    When the two columns disagree the longer period wins, because a block that
    is too short is what destroys the dependence structure; a slightly longer
    block only costs a little variety.
    '''
    periods = [s.period for s in seasonality if s.period]
    if not periods:
        return configured, (
            f'no seasonal cycle detected; using configured block_length={configured}')
    period = max(periods)
    if period == configured:
        return configured, f'configured block_length={configured} already matches detected period'
    # A period longer than the horizon would collapse every path onto a single block.
    effective = min(period, horizon)
    return effective, (
        f'aligned block_length {configured} -> {effective} '
        f'(detected {period}-week cycle)')


# --------------------------------------------------------------------------
# Incremental error statistics
# --------------------------------------------------------------------------

class RollingState:
    '''Sufficient statistics over the error history, updated in O(1) per week.

    The running sums give exact mean/variance, so appending a week never
    rescans history. Autocorrelation is deliberately *not* kept as an
    incremental cross-product: once a sliding window is in force the retained
    rows and any running cross-product drift apart, and a silently stale
    statistic is worse than a cheap recompute. It is instead recomputed from
    the retained window, which is bounded, so the cost stays amortised O(1).
    '''

    def __init__(self, max_lag: int = 26) -> None:
        if not 1 <= max_lag <= 104:
            raise ValueError('max_lag: integer in [1,104] required')
        self.max_lag = max_lag
        self.n = 0                               # total rows ever seen
        self._sum = np.zeros(2)
        self._sumsq = np.zeros(2)
        self._window: list[np.ndarray] = []      # retained for sampling + diagnostics
        self._window_cap: int | None = None

    @property
    def mean(self) -> np.ndarray:
        if self.n == 0:
            return np.zeros(2)
        return self._sum / self.n

    @property
    def variance(self) -> np.ndarray:
        if self.n < 2:
            return np.zeros(2)
        mean = self.mean
        return np.maximum(0.0, self._sumsq / self.n - mean ** 2)

    @property
    def std(self) -> np.ndarray:
        return np.sqrt(self.variance)

    def autocorrelation(self, lag: int) -> np.ndarray:
        '''Autocorrelation over the retained window at the given lag.'''
        if not 1 <= lag <= self.max_lag:
            return np.zeros(2)
        window = self.errors()
        if window.shape[0] <= lag + 1:
            return np.zeros(2)
        a = window[:-lag]
        b = window[lag:]
        ca, cb = a - a.mean(axis=0), b - b.mean(axis=0)
        denom = np.sqrt((ca ** 2).sum(axis=0) * (cb ** 2).sum(axis=0))
        return np.where(denom > 0, (ca * cb).sum(axis=0) / np.where(denom > 0, denom, 1.0), 0.0)

    def update(self, new_errors: Any) -> None:
        '''Append one or more weekly error rows: [actual - forecast].'''
        rows = _as_2d(new_errors, 'new_errors', cols=2)
        for row in rows:
            self.n += 1
            self._sum += row
            self._sumsq += row * row
            self._window.append(row.copy())
        if self._window_cap is not None and len(self._window) > self._window_cap:
            del self._window[: len(self._window) - self._window_cap]

    def set_window(self, cap: int | None) -> None:
        '''Cap the retained sampling window (sliding-window mode).

        The running sums still cover all history ever seen; only the rows used
        for sampling and diagnostics are capped. That is intentional: the plan
        is that old regimes stop driving the bootstrap, without pretending the
        long-run statistics never happened.
        '''
        if cap is not None and cap < 26:
            raise ValueError('window cap must be >= 26 weeks or None')
        self._window_cap = cap
        if cap is not None and len(self._window) > cap:
            del self._window[: len(self._window) - cap]

    def errors(self) -> np.ndarray:
        if not self._window:
            raise ValueError('no error history has been accumulated')
        return np.asarray(self._window, dtype=float)

    def snapshot(self) -> dict[str, Any]:
        return {
            'n': self.n,
            'mean': self.mean.tolist(),
            'std': self.std.tolist(),
            'window_retained': len(self._window),
            'window_cap': self._window_cap,
        }


# --------------------------------------------------------------------------
# Path simulation
# --------------------------------------------------------------------------

def _block_indices(
    rng: np.random.Generator,
    n_history: int,
    draws: int,
    block: int,
    horizon: int,
) -> np.ndarray:
    n_blocks = math.ceil(horizon / block)
    starts = rng.integers(0, n_history, size=(draws, n_blocks))
    return ((starts[:, :, None] + np.arange(block)) % n_history).reshape(draws, -1)[:, :horizon]


def resolve_bias(
    error_history: Any,
    debias: bool | str = 'auto',
    alpha: float = 0.05,
) -> tuple[np.ndarray, dict[str, Any]]:
    '''Decide how much of the mean error to remove before integrating it.

    `debias` may be True (always remove), False (never remove) or 'auto'.
    Auto is the default and is a per-column decision:

      * a mean that is statistically indistinguishable from zero is treated as
        sampling noise and removed, because carrying it along the path adds
        drift without adding signal;
      * a mean that survives the test is treated as a persistent property of
        the forecasting process and kept, because removing it would simply
        bias the forecast the other way.

    Either way the decision and its evidence are returned, never applied
    silently.
    '''
    hist = _as_2d(error_history, 'error_history', cols=2)
    mean = hist.mean(axis=0)
    if debias is True:
        return mean, {'mode': 'always', 'removed': mean.tolist(), 'p_values': None,
                      'note': 'configured to always remove the mean error'}
    if debias is False:
        return np.zeros(2), {'mode': 'never', 'removed': [0.0, 0.0], 'p_values': None,
                             'note': 'configured to keep the mean error'}
    if debias != 'auto':
        raise ValueError("debias: True, False or 'auto' required")

    structural, p_values = _mean_error_is_structural(hist, alpha)
    removed = np.array([0.0 if keep else m for keep, m in zip(structural, mean)])
    notes = []
    for j, label in enumerate(('receipts', 'payments')):
        if structural[j]:
            notes.append(f'{label}: mean {mean[j]:+.4f} is significant (p={p_values[j]:.4f}), kept')
        else:
            notes.append(
                f'{label}: mean {mean[j]:+.4f} not distinguishable from zero '
                f'(p={p_values[j]:.4f}), removed')
    return removed, {
        'mode': 'auto',
        'removed': removed.tolist(),
        'p_values': p_values,
        'structural': structural,
        'alpha': alpha,
        'note': '; '.join(notes),
    }


def simulate_paths(
    planned_inflow: Any,
    planned_outflow: Any,
    error_history: Any,
    *,
    initial_capacity: float,
    draws: int = 5000,
    block: int = 4,
    seed: int = 42,
    horizon: int = HORIZON,
    debias: bool | str = 'auto',
    shared_rng: np.random.Generator | None = None,
    alpha: float = 0.05,
) -> dict[str, Any]:
    '''Simulate future capacity paths around the 13-week plan.

    The mean historical error is resolved through `resolve_bias` rather than
    being removed unconditionally: an uncorrected mean of mu integrates to
    horizon * mu across the window, but a mean that reflects a real, ongoing
    bias of the forecast process must be kept or the correction overshoots.
    '''
    inflow = np.asarray(planned_inflow, dtype=float).ravel()
    outflow = np.asarray(planned_outflow, dtype=float).ravel()
    if inflow.shape != (horizon,) or outflow.shape != (horizon,):
        raise ValueError(f'planned inflow/outflow must each have {horizon} entries')
    if np.any(inflow < 0) or np.any(outflow < 0):
        raise ValueError('planned inflow/outflow must be nonnegative')
    hist = _as_2d(error_history, 'error_history', cols=2)
    if hist.shape[0] < 26:
        raise ValueError('error_history: at least 26 weekly rows required')
    if not 1000 <= draws <= 100000:
        raise ValueError('draws: integer in [1000,100000] required')
    if not 1 <= block <= hist.shape[0]:
        raise ValueError('block: integer in [1, len(error_history)] required')
    if not 0 <= seed <= 2 ** 32 - 1:
        raise ValueError('seed: integer in [0, 2**32-1] required')

    rng = shared_rng if shared_rng is not None else np.random.default_rng(seed)
    bias, bias_decision = resolve_bias(hist, debias, alpha)
    centred = hist - bias

    idx = _block_indices(rng, len(centred), draws, block, horizon)
    errors = centred[idx]

    receipts = np.maximum(0.0, inflow + errors[:, :, 0])
    payments = np.maximum(0.0, outflow + errors[:, :, 1])
    net = receipts - payments
    paths = np.column_stack(
        [np.full(draws, float(initial_capacity)),
         float(initial_capacity) + np.cumsum(net, axis=1)]
    )
    return {
        'paths': paths,
        'bias_removed': bias.tolist(),
        'bias_decision': bias_decision,
        'block': block,
        'draws': draws,
        'seed': seed,
        'debias': debias,
        'horizon': horizon,
    }


def summarise_paths(paths: np.ndarray, buffer: float) -> dict[str, float]:
    '''Gap statistics of the simulated capacity paths against the buffer.'''
    minimum = paths.min(axis=1)
    gap = np.maximum(0.0, float(buffer) - minimum)
    draws = paths.shape[0]
    k = max(1, math.ceil(0.05 * draws))
    worst = np.sort(gap)[-k:]
    return {
        'probability_shortfall_scenario': float(np.mean(gap > 0)),
        'monte_carlo_standard_error': float(
            math.sqrt(max(0.0, np.mean(gap > 0) * (1 - np.mean(gap > 0)) / draws))),
        'gap_p95': float(np.quantile(gap, 0.95, method='higher')),
        'gap_cvar95': float(worst.mean()),
        'mean_gap': float(gap.mean()),
        'median_gap': float(np.median(gap)),
        'mean_terminal_capacity': float(paths[:, -1].mean()),
    }


# --------------------------------------------------------------------------
# Walk-forward backtest
# --------------------------------------------------------------------------

@dataclass
class BacktestResult:
    origins: int
    mae: list[float] = field(default_factory=list)
    rmse: list[float] = field(default_factory=list)
    terminal_drift: list[float] = field(default_factory=list)
    coverage: float | None = None
    per_origin: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        def mean(xs: list[float]) -> float | None:
            return float(np.mean(xs)) if xs else None
        return {
            'origins': self.origins,
            'mae': mean(self.mae),
            'rmse': mean(self.rmse),
            'mean_terminal_drift': mean(self.terminal_drift),
            'max_abs_terminal_drift': float(np.max(np.abs(self.terminal_drift)))
            if self.terminal_drift else None,
            'coverage': self.coverage,
        }


def walk_forward_backtest(
    error_history: Any,
    panel: list[dict[str, Any]],
    *,
    warmup: int = 26,
    draws: int = 5000,
    configured_block: int = 4,
    seed: int = 42,
    debias: bool | str = 'auto',
    max_period: int = 26,
    min_strength: float = 0.30,
    window: int | None = None,
    alpha: float = 0.05,
) -> BacktestResult:
    '''Evaluate the forecaster out-of-sample across successive origins.

    `error_history` is a contiguous run of weekly [actual - forecast] rows
    indexed by *calendar* week, the same shape the v2 engine consumes. It is
    kept separate from `panel` on purpose: the panel holds 13-week blocks, and
    detecting seasonality on origin-concatenated blocks would report the block
    length (13) rather than any genuine business cycle.

    Origin o is scored using only error rows before o, so no future
    information leaks into the evaluation. Random draws are shared across
    origins (common random numbers), which cuts the variance of the
    origin-to-origin comparison and lets a smaller `draws` reach the same
    precision.
    '''
    hist = _as_2d(error_history, 'error_history', cols=2)
    if warmup < 26:
        raise ValueError('warmup: at least 26 weeks required before the first origin')
    if len(panel) <= warmup:
        raise ValueError('panel: more origins than the warmup window required')
    if hist.shape[0] < len(panel):
        raise ValueError('error_history: needs at least one row per origin')

    result = BacktestResult(origins=len(panel) - warmup)
    rng = np.random.default_rng(seed)

    for i, row in enumerate(panel):
        forecast = _as_2d(row['forecast'], f'panel[{i}].forecast', HORIZON, 2)
        actual = _as_2d(row['actual'], f'panel[{i}].actual', HORIZON, 2)
        if i < warmup:
            continue
        errors = hist[:i]
        if window is not None:
            errors = errors[-window:]
        seasonality = detect_seasonality(errors, max_period, min_strength)
        block, rationale = select_block_length(seasonality, configured_block)
        sim = simulate_paths(
            forecast[:, 0], forecast[:, 1], errors,
            initial_capacity=0.0, draws=draws, block=block, seed=seed,
            debias=debias, shared_rng=rng, alpha=alpha,
        )
        # paths.mean(axis=0) is already cumulative capacity; differencing
        # against the initial value yields the cumulative net flow directly.
        mean_path = sim['paths'].mean(axis=0)
        predicted_cumulative = mean_path[1:] - mean_path[0]
        actual_cumulative = np.cumsum(actual[:, 0] - actual[:, 1])
        diff = predicted_cumulative - actual_cumulative

        result.mae.append(float(np.mean(np.abs(diff))))
        result.rmse.append(float(np.sqrt(np.mean(diff ** 2))))
        result.terminal_drift.append(float(diff[-1]))
        result.decisions.append({
            'origin_index': i,
            'origin': row.get('origin', i),
            'block': block,
            'rationale': rationale,
            'seasonality': [s.note for s in seasonality],
            'bias': sim['bias_decision'],
        })
        result.per_origin.append({
            'origin': row.get('origin', i),
            'mae': float(np.mean(np.abs(diff))),
            'rmse': float(np.sqrt(np.mean(diff ** 2))),
            'terminal_drift': float(diff[-1]),
            'block': block,
        })

    if result.terminal_drift:
        drift = np.abs(result.terminal_drift)
        tolerance = float(np.percentile(drift, 90)) if len(drift) > 1 else float(drift[0])
        result.coverage = float(np.mean(drift <= tolerance))
    return result
